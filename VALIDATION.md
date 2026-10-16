# 0.4.0 复审与验证

日期：2026-10-16。本文件记录源码与自动化测试结果；部署及服务回读另行记录。

## 结果

| 范围 | 结果 | 边界 |
|---|---|---|
| Hermes v0.22.0 全套测试 | 372 passed | SDK 网络操作由测试替身接管；包含真实 PluginManager、ContextVar、GatewayStreamConsumer 边界及原 clarify resolver |
| Hermes v0.21.2 源码快照全套测试 | 298 passed, 52 skipped | 与运行服务器的关键文件哈希一致；缺少路由身份与新生命周期接口的集成用例明确跳过 |
| 服务器实际 Python 3.12 环境 | 298 passed, 52 skipped | 临时源码目录和独立 HERMES_HOME，使用当前运行版本的源码与依赖；不替代真实消息验收 |
| 离线预览新进程 | 通过 | 系统 Python 的 `-B -I` 进程，无 Hermes/SDK 导入、无网络；10 状态、12 文件 |
| 交叉审阅与差异检查 | 通过 | 已修复统计刷新、交互边界误判完成、附件覆盖失败状态、观察失效和卡片回收问题 |

本机出现 5 条上游弃用提示，服务器实际环境出现 2 条，无测试失败。

## 复审修复

- 所有观察查询必须有明确运行 profile；同一原始群消息在多个 bot 间不能借用状态。实际固定路由身份、session、message 和回合必须同时匹配。
- 停止回调进入原生会话锁后重新核验；绑定原处理 task、guard 和未结束的官方 ObservationRef。覆盖排队时原回合结束、任务交接复用 guard、同 task 内递归启动下一回合等反例。
- 话题原消息 ID 与回复锚点发生冲突时不猜测发起人。进程重启后的旧卡片、跨用户和跨会话点击均无控制权限。
- 澄清在官方 resolver 锁内读取和更新状态；超时、文本回答、取消先于发送 ACK 时，只在取得真实消息 ID 后更新原卡片。
- webhook 不消费同步卡片回包，改用原文本澄清；正常 default 接入后路由至其他 profile 的真实跨线程调度仍能绑定原生卡片。
- 控件首次可用时独立刷新布局，保留打字机效果；frame/watchdog 更新失败后互相重试，不把控件或页脚刷新误当成流式结束。
- profile 设置读取失败时使用安全默认值，不借用进程环境。

## 可复现命令

在仓库根目录使用已有 Hermes 依赖和 pytest 的 Python：

```bash
PYTHONDONTWRITEBYTECODE=1 HERMES_AGENT_ROOT=/path/to/hermes-agent \
  /path/to/test-environment/bin/python -m pytest -q -p no:cacheprovider tests

python3 -B -I tools/preview_cards.py --output /tmp/cardkit-preview
```

两个版本均运行完整 `tests`；旧版本跳过原因由测试的能力检测明确给出，基础卡片、文本回退、布局与纯观察存储测试继续执行。

## 关键验收边界

- 插件仍继承内置飞书适配器并注册平台；没有侧车、Hermes 源码补丁或额外模型调用。
- 无法证明 profile、原始入站消息和回合关联时，不显示推测统计。缺少精确回合、任务或会话绑定时隐藏停止按钮；文字 `/stop` 仍沿用原流程。
- 审批继续使用原 Hermes 流程；原生卡片扩展覆盖 websocket 单选澄清。多选、开放式问题、webhook 和旧版缺能力路径沿用原文本交互。
- 续答导航只依赖合法且匹配的真实 IM 回执。网络成功但回执丢失时，继承的上游重试仍可能重复送达。
- HTML 为结构示意。长中文单元格、极宽表格、自动行高、手机显示与真实按钮点击，尚未做飞书客户端验收。
- 自动化测试不发送真实消息、不改变运行配置；安装切换、服务加载与真实客户端点击分别验收。
