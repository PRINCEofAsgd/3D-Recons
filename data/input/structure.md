# 输入数据包

```text
input/<dataset>/
├── screenshots/
├── cali.txt 或 cali.json
├── scale.txt 或 scale.json
├── floorplan.png/.jpg/.jpeg
└── external_calibration/          # 可选、已授权参数候选
    ├── intrinsic/<camera_id>.yml
    ├── extrinsic/<camera_id>.yml
    └── registration.json
```

`cali` 与 `scale` 建立图像、平面图和米制地面的关系。相机 ID 必须在截图、标定和可选外部参数中一致。

外部 K/D/R/t 只有完成单位、轴向、外参方向、共同世界系与目标地面验收后，才能在 `registration.json` 中声明为已验证。未验收参数只可导入为 `ready_provisional`，输出必须标记为 `geometric_trial`。

输入目录不得包含账户信息、真实业务名称、无关时间戳或其他项目来源记录；程序不得写回输入文件。
