# 可 fork 源码 Source Kit

配置 Handoff 与源码交付是两个不同产品层：

- Handoff 保存某个 OPC 的 Box 规格、严格配置、编译工件、部署模板和激活指南；
- Source Kit 保存完整可编辑产品源码、测试、Pack、三类样板、文档、前端、脚本和 CI 起点。

生成与验证：

```bash
opc-finance-box source-kit-bundle \
  --output /absolute/delivery/opc-finance-box-source-kit.zip
opc-finance-box source-kit-verify \
  /absolute/delivery/opc-finance-box-source-kit.zip
opc-finance-box source-kit-unpack \
  /absolute/delivery/opc-finance-box-source-kit.zip \
  /absolute/new/opc-finance-box-fork \
  --actor RECIPIENT
opc-finance-box source-kit-unpack-verify \
  /absolute/new/opc-finance-box-fork
```

输出采用排他创建并默认写成 `0600`，不会覆盖已有文件。验证器限制归档、成员数量、单文件和总内容大小；拒绝 ZIP 前置/尾随数据、危险或重复路径、符号链接式元数据、非固定时间戳和非规范压缩，并逐成员重算大小与 SHA-256。随后它使用当前安装的源码与资源重新构建全部逻辑成员，内部自洽但不属于当前版本的改写也会失败。

## 严格内容白名单

Source Kit 只包含：

- 顶层 `.gitignore`、`LICENSE`、`README.md`、`SECURITY.md`、`design-qa.md`、`pyproject.toml` 和 `run.py`；
- `src/` 完整 Python 源码与 `tests/` 全量测试；
- `packs/`、`examples/`、`docs/`、`public/`、`box/`、`evals/`、`deployment/`、`skills/`、`scripts/`；
- `.github/workflows/tests.yml`；
- `data/commerce_demo.json` 和 `data/demo_scenarios.json` 两个明确 demo 数据文件；
- 生成的 `SOURCE-KIT.md` 和逐文件 `source-kit-manifest.json`。

目录规则同时限制文件扩展名；白名单目录里出现未知类型或符号链接会使构建失败，而不是静默跟随。`__pycache__` 和 `node_modules` 被明确视为可重建依赖/缓存并排除。

明确不包含：

- `.git/` 历史、分支和本机 Git 配置；
- `.venv/`、`node_modules/` 或其他 vendored dependencies；
- `build/`、`dist/`、`outputs/`、`.tmp/` 和临时目录；
- `.opc-finance-data/`、ledger、Pipeline runs、Connector sync、Agent runtime；
- 首客私有工作区、真实财务源文件、复核件、凭据或环境变量值。

manifest 对这些边界使用显式布尔字段，不用“未发现”代替契约保证。Source Kit 目前不包含依赖 lock；fork 接收方应依据自己的 Python 镜像、包源和供应链策略生成并审计 lock。

## fork 后验证

先完成 `source-kit-verify`，再使用 `source-kit-unpack` 初始化到一个不存在的绝对目录。它读取一次归档，在内存中完成同等级别验证，并且只把这一次验证过的成员字节写入目标；不会调用通用 ZIP extract、不会执行成员、不会覆盖或合并目录，也不会删除源包。所有目录使用 `0700`、文件使用 `0600`，最后写入非签名收据。`source-kit-unpack-verify` 不需要原 ZIP，可重算完整树、收据和当前安装资源的可复现性。

离线重验只针对“刚初始化、尚未修改”的原始工作区。通过后即可按自己的 Fork 流程调整权限、编辑源码、安装依赖或执行 `git init`；这些动作会有意使原始收据验证失败，而不应通过重写收据伪装成原始交付。进入新目录后运行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests
python -m src.cli pack-audit
python -m src.cli eval evals/core_packs.json
python run.py
```

Source Kit 没有 Git 历史，但目录结构可直接执行 `git init` 后建立购买方自己的私有仓库。不要把归档 SHA-256 当成发布者公钥签名；需要来源身份保证时，应在交付通道外再附签名或受信任制品库证明。

安全初始化和离线重验的完整合同见[可 Fork 源码安全初始化](可Fork源码安全初始化.md)。

完整源码与测试只证明产品可审计、可修改、可重新构建。它不把任何 Pack 升级为 stable，不代表真实 OPC 已完成 Shadow Close，也不授权入账、付款、关账或税务申报。
