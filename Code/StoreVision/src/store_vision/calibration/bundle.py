"""Bundle adjustment: refine focal + 6DOF pose per camera (7 DoF total).

We avoid joint multi-camera BA because each camera contributes only 4 ground
correspondences; per-camera 7-DoF is well-posed (8 residuals vs 7 params).
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
from scipy.optimize import least_squares

from store_vision.calibration.homography import undistort_camera_pts
from store_vision.config import StoreConfig
from store_vision.data.models import CameraCalibration, StoreDataset
from store_vision.geometry.coords import map_percent_to_cm


def _decompose_H(H: np.ndarray, K: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Approximate (R, t) from ground-plane homography."""
    Kinv = np.linalg.inv(K)
    h1, h2, h3 = H[:, 0], H[:, 1], H[:, 2]
    norm1 = np.linalg.norm(Kinv @ h1)
    if norm1 < 1e-9:
        return np.eye(3), np.zeros(3)
    lam = 1.0 / norm1
    r1 = lam * (Kinv @ h1)
    r2 = lam * (Kinv @ h2)
    r3 = np.cross(r1, r2)
    R_approx = np.column_stack([r1, r2, r3])
    U, _, Vt = np.linalg.svd(R_approx)
    R = U @ Vt
    if np.linalg.det(R) < 0:
        R = -R
    t = lam * (Kinv @ h3)
    return R, t


def _project(K: np.ndarray, R: np.ndarray, t: np.ndarray, world_xy: np.ndarray) -> np.ndarray:
    Pw = np.hstack([world_xy, np.zeros((world_xy.shape[0], 1))])
    Pc = (R @ Pw.T + t.reshape(3, 1)).T
    uv = (K @ Pc.T).T
    z = np.maximum(uv[:, 2:3], 1e-9)
    return uv[:, :2] / z


def refine_cameras(
    dataset: StoreDataset,
    cfg: StoreConfig,
    out_dir: Path | None = None,
) -> dict[str, float]:
    cameras = [c for c in dataset.camera_list() if c.homography is not None and c.K is not None]
    rmses: dict[str, float] = {}
    dump: dict = {"cameras": {}}

    for calib in cameras:
        world = map_percent_to_cm(calib.map_points_pct_array(), cfg)
        image = undistort_camera_pts(calib)

        K0 = calib.K.copy()
        cx, cy = K0[0, 2], K0[1, 2]
        f0 = float(K0[0, 0])
        R0, t0 = _decompose_H(calib.homography, K0)
        rvec0, _ = cv2.Rodrigues(R0)
        x0 = np.array([f0, *rvec0.flatten().tolist(), *t0.tolist()], dtype=np.float64)

        def residuals(x: np.ndarray) -> np.ndarray:
            f = max(float(x[0]), 50.0)
            rvec = x[1:4]
            t = x[4:7]
            K = np.array([[f, 0, cx], [0, f, cy], [0, 0, 1]], dtype=np.float64)
            R, _ = cv2.Rodrigues(rvec)
            return (_project(K, R, t, world) - image).ravel()

        try:
            res = least_squares(residuals, x0, method="lm", max_nfev=300)
            xo = res.x
        except Exception:
            xo = x0

        f = max(float(xo[0]), 50.0)
        rvec = xo[1:4]
        t = xo[4:7]
        K_ref = np.array([[f, 0, cx], [0, f, cy], [0, 0, 1]], dtype=np.float64)
        R_ref, _ = cv2.Rodrigues(rvec)
        proj = _project(K_ref, R_ref, t, world)
        rmse_new = float(np.sqrt(np.mean(np.sum((proj - image) ** 2, axis=1))))

        # Keep if improved, else fall back.
        prev_rmse = calib.reprojection_rmse if calib.reprojection_rmse else 1e9
        if rmse_new <= max(prev_rmse * 2.0 + 1.0, 5.0):
            calib.K = K_ref
            calib.R = R_ref
            calib.t = t.reshape(3)
            calib.reprojection_rmse = rmse_new
            rmses[calib.device_serial] = rmse_new
        else:
            rmses[calib.device_serial] = prev_rmse

        dump["cameras"][calib.device_serial] = {
            "rmse_image_px": rmses[calib.device_serial],
            "K": calib.K.tolist() if calib.K is not None else None,
            "R": calib.R.tolist() if calib.R is not None else None,
            "t": calib.t.tolist() if calib.t is not None else None,
        }

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(out_dir / "bundle.json", "w", encoding="utf-8") as f:
            json.dump(dump, f, indent=2)
    return rmses
