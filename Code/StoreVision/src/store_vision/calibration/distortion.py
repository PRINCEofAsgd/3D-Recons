"""同型号鱼眼镜头的共享 K/D 标定，以及鱼眼去畸变辅助函数。"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
from scipy.optimize import least_squares

from store_vision.calibration.fisheye_bundle import (
    FisheyeBundleConfig,
    fit_shared_fisheye_bundle,
)
from store_vision.calibration.intrinsics_validation import (
    provisional_fisheye_intrinsics,
)
from store_vision.data.models import CameraCalibration, StoreDataset
from store_vision.geometry.coords import map_percent_to_floor_px
from store_vision.resolution import canonicalize_homographies


def _default_K(w: int, h: int) -> np.ndarray:
    f = max(w, h) * 0.85
    return np.array([[f, 0, w / 2.0], [0, f, h / 2.0], [0, 0, 1]], dtype=np.float64)


def undistort_points(
    pts: np.ndarray,
    K: np.ndarray,
    dist: np.ndarray,
) -> np.ndarray:
    pts = np.asarray(pts, dtype=np.float64).reshape(-1, 1, 2)
    coefficients = np.asarray(dist, dtype=np.float64).reshape(-1)
    if coefficients.size == 4:
        out = cv2.fisheye.undistortPoints(
            pts, K, coefficients.reshape(4, 1), P=K
        )
    else:
        # 只为读取旧输出保留普通 OpenCV 畸变兼容；新路线始终写四维鱼眼 D。
        out = cv2.undistortPoints(pts, K, coefficients, P=K)
    return out.reshape(-1, 2)


def undistort_image(
    img: np.ndarray,
    K: np.ndarray,
    dist: np.ndarray,
) -> np.ndarray:
    coefficients = np.asarray(dist, dtype=np.float64).reshape(-1)
    if coefficients.size != 4:
        return cv2.undistort(img, K, coefficients, None, K)
    height, width = img.shape[:2]
    map_x, map_y = cv2.fisheye.initUndistortRectifyMap(
        K,
        coefficients.reshape(4, 1),
        np.eye(3, dtype=np.float64),
        K,
        (width, height),
        cv2.CV_32FC1,
    )
    return cv2.remap(
        img,
        map_x,
        map_y,
        interpolation=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
    )


def distort_points(
    pts: np.ndarray,
    K: np.ndarray,
    dist: np.ndarray,
) -> np.ndarray:
    """把去畸变像素点重新映射到原始鱼眼图像像素。"""

    points = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
    coefficients = np.asarray(dist, dtype=np.float64).reshape(-1)
    if coefficients.size != 4:
        try:
            return cv2.distortPoints(
                points.reshape(-1, 1, 2), K, coefficients, P=K
            ).reshape(-1, 2)
        except (AttributeError, cv2.error):
            return points
    homogeneous = np.column_stack([points, np.ones(len(points))])
    normalized = (np.linalg.inv(K) @ homogeneous.T).T[:, :2]
    distorted = cv2.fisheye.distortPoints(
        normalized.reshape(-1, 1, 2),
        K,
        coefficients.reshape(4, 1),
    )
    return distorted.reshape(-1, 2)


def _image_size(calib: CameraCalibration) -> tuple[int, int]:
    w, h = 1920, 1080
    if calib.image_path:
        img = cv2.imread(calib.image_path)
        if img is not None:
            h, w = img.shape[:2]
    return w, h


def _estimate_one(
    calib: CameraCalibration,
    floor_w: int,
    floor_h: int,
) -> tuple[np.ndarray, np.ndarray, float]:
    w, h = _image_size(calib)
    K = _default_K(w, h)
    src = calib.camera_points_array()
    dst = map_percent_to_floor_px(calib.map_points_pct_array(), floor_w, floor_h)

    def residuals(params: np.ndarray) -> np.ndarray:
        k1, k2 = float(params[0]), float(params[1])
        d = np.array([k1, k2, 0.0, 0.0, 0.0], dtype=np.float64)
        und = undistort_points(src, K, d)
        H, _ = cv2.findHomography(und, dst, method=0)
        if H is None:
            return np.ones(8, dtype=np.float64) * 1e6
        proj = cv2.perspectiveTransform(und.reshape(-1, 1, 2), H).reshape(-1, 2)
        return (proj - dst).ravel()

    res = least_squares(residuals, np.zeros(2), method="lm", max_nfev=200)
    k1, k2 = float(res.x[0]), float(res.x[1])
    dist = np.array([k1, k2, 0.0, 0.0, 0.0], dtype=np.float64)
    rmse = float(np.sqrt(np.mean(residuals(res.x).reshape(-1, 2) ** 2)))
    return K, dist, rmse


def _calibrate_legacy_distortion(
    dataset: StoreDataset,
    out_dir: Path | None = None,
) -> dict[str, float]:
    """保留旧结果审计所需的逐机普通径向拟合，不作为公开或正式路线。"""
    # Without a real floor plan we still need *some* destination size for the
    # least-squares projection. We use a reasonable default; downstream
    # homography becomes meaningful only after a real plan is provided.
    fw, fh = dataset.floor_plan_size or (1600, 900)
    rmses: dict[str, float] = {}
    dump: dict = {}
    for calib in dataset.camera_list():
        K, dist, rmse = _estimate_one(calib, fw, fh)
        calib.K = K
        calib.dist_coeffs = dist
        rmses[calib.device_serial] = rmse
        dump[calib.device_serial] = {
            "K": K.tolist(),
            "dist": dist.tolist(),
            "rmse_floor_px": rmse,
            "image_size": list(_image_size(calib)),
        }
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_dir / "distortion.json", "w", encoding="utf-8") as f:
            json.dump(dump, f, indent=2)
    return rmses


def calibrate_shared_fisheye(
    dataset: StoreDataset,
    out_dir: Path | None = None,
    *,
    config: FisheyeBundleConfig | None = None,
) -> dict:
    """共享拟合全部相机的鱼眼 K/D 和位姿，并自动执行同数据联合 BA。"""

    rows: list[dict] = []
    cameras: list[CameraCalibration] = []
    for calibration in dataset.camera_list():
        width, height = _image_size(calibration)
        percent = calibration.map_points_pct_array()
        # 与共享标定世界系保持一致：X 向平面图右，Y 向平面图上。
        # 完整 pipeline 不依赖绝对平移尺度，因此无 scale 时使用百分比单位。
        world = np.column_stack([percent[:, 0], -percent[:, 1]])
        image = calibration.camera_points_array()
        homography, _ = cv2.findHomography(world, image, method=0)
        if homography is None or not np.isfinite(homography).all():
            continue
        rows.append(
            {
                "physical_camera_id": calibration.device_serial,
                "image_name": (
                    Path(calibration.image_path).name
                    if calibration.image_path
                    else calibration.name
                ),
                "image_resolution": [width, height],
                "source_world_points_metres": world,
                "source_image_points_px": image,
                "H_world_plane_to_image": homography / homography[2, 2],
            }
        )
        cameras.append(calibration)
    if not rows:
        raise ValueError("没有可用于共享鱼眼标定的相机控制点")

    canonical_size, canonical_rows = canonicalize_homographies(rows)
    fitted = fit_shared_fisheye_bundle(
        canonical_rows,
        canonical_size,
        config=config
        or FisheyeBundleConfig(
            minimum_camera_count=1,
            initialization_max_nfev=400,
            bundle_adjustment_max_nfev=600,
        ),
    )
    fitted_k = np.asarray(fitted["shared_K"], dtype=np.float64)
    fitted_d = np.asarray(fitted["shared_D"], dtype=np.float64)
    fitted_validation = dict(fitted["intrinsics_validation"])
    canonical_k = fitted_k
    shared_d = fitted_d
    distortion_scale = 1.0
    active_validation = fitted_validation
    mapping_route = "credible_fitted_fisheye"
    if not fitted_validation["usable_for_sfm"]:
        # 完整 2.5D 流水线也不得继续用发生折返的 K/D。没有独立内参时，
        # 与 SfM 保持相同的透明降级：初始 K + 缩放非零 D，仅供布局试算。
        (
            canonical_k,
            shared_d,
            distortion_scale,
            active_validation,
        ) = provisional_fisheye_intrinsics(
            fitted["initialization"]["initial_K"],
            fitted_d,
            canonical_size,
        )
        mapping_route = "provisional_initial_k_scaled_nonzero_d"
    poses = {
        row["physical_camera_id"]: row for row in fitted["poses"]
    }
    camera_dump: dict[str, dict] = {}
    row_by_id = {
        row["physical_camera_id"]: row for row in canonical_rows
    }
    for calibration in cameras:
        row = row_by_id[calibration.device_serial]
        native_k = (
            np.linalg.inv(np.asarray(row["image_to_canonical_transform"]))
            @ canonical_k
        )
        native_k /= native_k[2, 2]
        pose = poses[calibration.device_serial]
        calibration.K = native_k
        calibration.dist_coeffs = shared_d.copy()
        calibration.R = np.asarray(pose["R"], dtype=np.float64)
        calibration.t = np.asarray(pose["T_metres"], dtype=np.float64)
        calibration.reprojection_rmse = float(
            pose["pose_reprojection_rmse_px"]
        )
        calibration.camera_model = "OPENCV_FISHEYE"
        camera_dump[calibration.device_serial] = {
            "camera_model": calibration.camera_model,
            "K": native_k.tolist(),
            "D": shared_d.tolist(),
            "R": calibration.R.tolist(),
            "t": calibration.t.tolist(),
            "rmse_image_px": calibration.reprojection_rmse,
            "image_size": row["image_resolution"],
        }

    result = {
        "camera_model": "OPENCV_FISHEYE",
        "same_camera_model_assumption": True,
        "canonical_resolution": list(canonical_size),
        "shared_K": canonical_k.tolist(),
        "shared_D": shared_d.tolist(),
        "fitted_K": fitted_k.tolist(),
        "fitted_D": fitted_d.tolist(),
        "intrinsics_validation": fitted_validation,
        "active_intrinsics_validation": active_validation,
        "mapping_intrinsics_route": mapping_route,
        "distortion_scale": distortion_scale,
        "credible_calibration": bool(
            mapping_route == "credible_fitted_fisheye"
            and fitted_validation["usable_for_sfm"]
        ),
        "fitted_initialization": fitted["initialization"],
        "bundle_adjustment": fitted["bundle_adjustment"],
        "cameras": camera_dump,
    }
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        serializable = json.loads(
            json.dumps(
                result,
                default=lambda value: (
                    value.tolist()
                    if isinstance(value, np.ndarray)
                    else float(value)
                    if isinstance(value, np.generic)
                    else value
                ),
            )
        )
        (out_dir / "distortion.json").write_text(
            json.dumps(
                {
                    "camera_model": "OPENCV_FISHEYE",
                    "shared_K": serializable["shared_K"],
                    "shared_D": serializable["shared_D"],
                    "fitted_K": serializable["fitted_K"],
                    "fitted_D": serializable["fitted_D"],
                    "intrinsics_validation": serializable[
                        "intrinsics_validation"
                    ],
                    "active_intrinsics_validation": serializable[
                        "active_intrinsics_validation"
                    ],
                    "mapping_intrinsics_route": serializable[
                        "mapping_intrinsics_route"
                    ],
                    "distortion_scale": serializable["distortion_scale"],
                    "credible_calibration": serializable[
                        "credible_calibration"
                    ],
                    "cameras": serializable["cameras"],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (out_dir / "bundle.json").write_text(
            json.dumps(serializable, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return result
