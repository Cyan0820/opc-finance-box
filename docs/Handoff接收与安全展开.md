# Handoff 接收与安全展开

`handoff-bundle` 交付的是一个可编辑 Box 候选工作区，不是源码发行包、财务证据或上线批准。接收方先安装通过 `distribution-verify` 的同版本 wheel，或进入已 clone 的 OPC Finance Box repo，再验证和展开：

```bash
opc-finance-box handoff-verify /absolute/private/customer-handoff.zip
opc-finance-box handoff-unpack /absolute/private/customer-handoff.zip \
  /absolute/new/handoff-workspace --actor HANDOFF_RECIPIENT
opc-finance-box handoff-unpack-verify /absolute/new/handoff-workspace
```

若配置包来自浏览器 Builder，同时下载页面生成的 `.browser-receipt.json`。浏览器下载文件通常继承下载目录的公开权限，正式验证器会拒绝它；先显式收紧两个文件，再联合验证：

```bash
chmod 600 /absolute/private/customer-handoff.zip \
  /absolute/private/customer-handoff.browser-receipt.json
opc-finance-box handoff-verify /absolute/private/customer-handoff.zip
opc-finance-box handoff-receipt-verify \
  /absolute/private/customer-handoff.zip \
  /absolute/private/customer-handoff.browser-receipt.json
```

联合验证要求收据为单硬链接、非符号链接、`0600` 的普通 JSON 文件，并严格匹配正式验证所得的 ZIP 文件名、长度、SHA-256、runtime fingerprint、manifest schema 和文件数。它不会信任收据来跳过 Pack 可复现验证。通过只表示“这份收据描述了这份已验证 ZIP”；收据可由任何能读取 ZIP 的人重新构造，因此 `browser_execution_attested=false`，它不是浏览器身份、发行方身份或数字签名证明。

输入 ZIP 和目标父目录必须已经存在；目标工作区必须是尚不存在的绝对路径。命令不会覆盖、合并或清理已有目录，不会删除源 ZIP。展开后的目录是配置、编译工件、部署模板和首客指南工作区；它不包含完整产品源码。Docker 构建仍需完整 starter repo，或者在 wheel 安装环境中使用 systemd/CLI 入口。

## 执行顺序

1. 通过 no-follow 文件描述符把 owner-private ZIP 读入内存。
2. 在创建目标目录前完成 `handoff-verify` 的全部归档、manifest、成员哈希、编译锁和当前 Pack 重建检查；尾随/前置 polyglot 数据也会被拒绝。
3. 使用同一份已验证内存字节创建 `0700` 新目录，逐成员以 `0600`、exclusive create 写入；不调用 ZIP 的通用 extract，也不执行任何归档成员。
4. 每个文件完成 flush/fsync 后，最后写入 `handoff-unpack-receipt.json`，再重新扫描整个目录并按当前 Pack 重建所有成员。
5. 只有收据存在且 `handoff-unpack-verify` 通过，目录才可被视为完整展开。接着仍要运行 `validate`、`compile`、`doctor` 和适用的 activation 流程。

如果磁盘、权限或进程在中途失败，工具会保留已经创建的目标目录，但不会保留完成收据。它不会自动删除可能需要取证的文件，也不会在重试时复用该目录；操作者应保留或按自身变更流程处置该未完成目录，并选择一个新的目标路径重试。

## 收据合同

私有收据记录：

- 源 ZIP SHA-256、字节数和 runtime fingerprint；
- 原归档成员数、manifest 文件数和逐成员路径/大小/SHA-256；
- 安装树 content fingerprint、收据 payload fingerprint、接收执行人和 UTC 时间；
- 验证先于展开、当前 Pack 可复现、目标此前不存在、无覆盖、未执行成员、未删除源包等边界。

CLI 结果不会返回目标路径、执行人、Box 规格、财务值或凭据。收据是本地完整性与操作连续性记录，不是数字签名；能访问目录的本机用户仍可能同时改写文件和收据。因此 `receipt_is_digital_signature=false`、`authoritative_financial_evidence=false`，它不能替代软件供应链签名、财务复核、Connector Shadow、稳定版晋级或申报批准。

## 重新验证

`handoff-unpack-verify` 不需要保留源 ZIP。它会：

- 拒绝非 `0700` 目录、非 `0600` 文件、硬链接、符号链接、额外/缺失文件或目录；
- 限制成员数量、单文件大小和总内容大小，不信任收据声明进行无界读取；
- 重算收据 payload、安装树 fingerprint 和逐文件哈希；
- 重新绑定 manifest、`compiled/box.lock.json` 和 runtime fingerprint；
- 从 `box-spec.json` 使用当前安装 Pack 重新生成并逐成员比较全部内容。

若当前压缩库能复现源 ZIP 的完全相同字节，结果会额外返回 `source_bundle_sha_matches_current_builder=true`。该字段为压缩归档一致性信息；内容与 Pack 绑定才是展开验证的必需条件。
