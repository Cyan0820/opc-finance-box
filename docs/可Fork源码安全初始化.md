# 可 Fork 源码安全初始化

`source-kit-unpack` 把已经通过严格 Source Kit 合同的归档落成一个全新、私有、可继续 Fork 的源码工作区。它解决的是验证与解压之间的文件替换、路径穿越、覆盖已有目录和“半写入却被当作完成”等接收端风险，不负责执行源码、安装依赖或创建 Git 历史。

## 初始化

```bash
opc-finance-box source-kit-unpack \
  /absolute/delivery/opc-finance-box-source-kit.zip \
  /absolute/new/opc-finance-box-fork \
  --actor RECIPIENT

opc-finance-box source-kit-unpack-verify \
  /absolute/new/opc-finance-box-fork
```

目标必须是不存在的绝对目录，父目录必须已经存在且不是符号链接。命令不会覆盖、清空、合并或重用目标；验证归档失败时不会创建目标。归档通过 no-follow descriptor 有界读入后，canonical ZIP、manifest、逐成员哈希和当前安装资源复现都针对同一份内存字节完成，随后才开始写入。

根目录与全部子目录使用 `0700`，每个源码文件使用 no-follow、exclusive create 写为 `0600` 并 fsync。工具不调用通用 ZIP 解压，不执行任何成员，不继承秘密，不安装依赖，不运行测试，不执行 `git init`，也不删除源 ZIP。

## 完成收据

`.opc-source-kit-unpack-receipt.json` 是最后写入的文件。它记录：

- 源归档 SHA-256、大小与 Source Kit content fingerprint；
- manifest 文件数、全部归档成员的大小和 SHA-256、完整树 fingerprint；
- 接收 actor 和 UTC 初始化时间；
- 归档已先验证、可由当前安装资源复现、目标此前不存在且没有覆盖；
- 未执行成员、未安装依赖、未创建 Git 历史、未持久化凭证/私有证据、未添加财务数据、未执行外部动作。

写入任一步失败时，收据不会存在或会被移除。部分目录会保留供操作者按自己的安全流程处理，但工具不会重用它，也不会把它报告为完成。

## 离线重验

`source-kit-unpack-verify` 不需要原 ZIP。它拒绝：

- 非 `0700` 的目录、非 `0600` 或多硬链接的文件；
- 符号链接、特殊文件、额外或缺失的文件/目录；
- 失效、缺失或被改写的收据；
- 任何文件大小、SHA-256、树 fingerprint 或 manifest 绑定变化；
- 不能由当前安装源码资源逐成员重建的工作区。

离线重验是“原始交付已完整落地”的一次性接收门禁。验证通过后，用户通常会编辑源码、改变协作权限、创建 `.venv`、运行测试并初始化 `.git`，届时原始树自然不再匹配。请保留原 Source Kit 和外部交付哈希作为来源记录，不要修改收据来制造虚假的原始状态。

## 非授权边界

收据只是本地完整性记录，不是发布者公钥签名；有目录写权限的人能够同时篡改文件和收据。需要身份真实性时，应额外使用签名、受信任制品库或独立交付通道。

安全初始化不代表依赖已审计、测试已运行、Pack 已晋级 stable，也不是任何真实财务证据。它不授权入账、付款、关账或税务申报，不会改变活动运行时。
