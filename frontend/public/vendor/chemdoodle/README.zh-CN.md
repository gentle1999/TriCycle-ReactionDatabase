# ChemDoodle Web Components 本地说明

[English upstream note](README.md) | [简体中文说明](README.zh-CN.md)

此目录包含 ChemDoodle Web Components `11.0.0` 的未修改上游发行文件。上游 archive 地址、
SHA-256、GPL-3.0-or-later 许可证和每个文件 hash 均记录在英文说明及 `manifest.json` 中。
`COPYING.txt` 是上游许可证原文，保持不翻译且不修改。

受审计的上游资源包括 `ChemDoodleWeb.js`、`ChemDoodleWeb.css`、`ChemDoodleWeb-uis.js` 和
`COPYING.txt`。版本 11 的 `SketcherCanvas` UI bundle 不依赖 jQuery、jQuery UI 或 Touch Punch；
这些历史兼容文件有意不进入 sandbox editor 或主 Vue 应用。

从仓库根目录运行以下命令，以核验本地文件、manifest 和具备 OSV package identity 的组件：

```bash
uv run --frozen python scripts/audit_vendored_assets.py
```

ChemDoodle 是 direct archive，并非 OSV ecosystem package。因此可执行资源的供应链控制依赖于
上游 archive hash 与 `manifest.json` 中的逐文件 hash。本文只是本地中文说明；不改变上游资源。
