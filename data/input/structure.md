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

文件名统一为上面的 `cali`、`scale`、`floorplan` 与 `screenshots`；截图文件名必须保留与标定对应的相机 ID。旧 `decathon` 截图尾部的采集时间戳已移除，图像内容未改。CAD 原始辅助资料可随所属输入包保存，但不参与程序加载。

当前从本机源目录恢复的九组数据均属于原始输入层，没有 `manifest.json`，不可当作中间层。`apple_bj_fyh` 与 `decathon` 缺少 `scale`，输入预检会阻止直接运行；其他七组具备四类必需文件。`apple_exam` 是版本库中保留的旧格式示例，缺少 `scale`，不作为完整桌面输入。真实输入包默认只存在本地并被 Git 忽略。

输入目录不得包含账户信息、无关时间戳或其他项目来源记录；程序不得写回输入文件。
