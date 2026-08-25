# Aqours_code v0.1 收口清单

本清单只记录面试作品集发布前的主链路事项。Experimental 功能的完整性、GUI、长期 Memory、自动 Worktree 合并和大规模架构重构不属于 v0.1 blocker。

- [x] 在全新 Python 3.11 虚拟环境中执行 `pip install -e .`，安装成功。
- [x] 使用完整但不发起网络请求的测试配置启动 CLI，能显示 workspace/provider/model 且不输出 API Key。
- [x] `Aqours_code --help` 和 `python -m aqours_code --help` 均在独立虚拟环境中正常返回。
- [x] 注入 scripted model 后，Agent 主链路能完成基础文件修改、工具调用和测试执行场景。
- [x] Context Compact 的根任务保留、工具调用对、失败回退和重复压缩行为通过回归测试。
- [x] 权限拒绝、测试失败、constraint violation 和 grader 失败不会被错误宣布为成功。
- [x] Trace 能记录模型/工具过程和 v0.1 运行元数据，并对 Key、Header 与 Base URL 认证信息脱敏。
- [x] Agent Context 默认硬上限为 200K、80K 触发 Compact；摘要输入与输出预算独立配置。
- [x] 移除未进入 Context/决策链、但会反复扫描 Workspace 的 RunKnowledge 状态机；重复读取指标继续由 Trace 提供。
- [x] GitHub Actions 在 Python 3.11 上执行安装、编译、完整 pytest 和 CLI smoke，并补充 MIT License。
- [x] 删除已被当前文档替代的历史功能审计、旧临时 dump 脚本和对应死代码/测试；保留 Eval、Trace、权限与协作主链路。
- [ ] 使用一组真实用户凭据执行最小 Coding Task；本次未读取现有秘密，也未发起真实模型调用。
- [ ] 在可用 Docker daemon 上按 README 命令完成 `read_file_basic` scripted smoke；当前 Docker daemon 未启动，因此 README 的 Docker 命令尚未实机验证。
- [ ] 在 GitHub 将仓库名从当前远端的 `Aqours-code` 统一为正式名称 `Aqours_code`；本次按要求未 commit、未 push，也未修改远端仓库。
