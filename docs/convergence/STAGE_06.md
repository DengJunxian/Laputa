# 阶段 06 · 联合世界研究工作台与工程门

**本地验收 PASS，GitHub 托管运行 PENDING。** 验证源码 `548cd15248610da9e43ebc7fa3917232a7a8ff1a`。现有 Workbench 增量接入联合证券冲击配置、提交期次、主体检查、关系图和阶段 05 对照。沿用现有执行器、提交归档及 HTTP 授权，依赖增量为零。

## 从工作台完成一次研究运行

从仓库根运行已有命令，打开进程打印的本地研究者地址。`--joint-pilot` 指向已有阶段 05 归档，可省略以单独运行联合配置。

```sh
.venv/bin/ecosim workbench --workspace build/convergence/06/researcher \
  --research-preview examples --joint-pilot build/convergence/05/pilot --port 0
```

1. 点击「冻结联合配置」和「启动」。服务器按当前登记配置的完整哈希冻结计划，运行 8 户、2 企业、2 家银行、1 财政主体及 1 类证券。第 5 期完成后可逐期检查。时间轴区分已提交合成标价和当期实际收盘，并展示消费、分企业销售额、实付工资、准备金和国库现金。
2. 查看第 3 期的「多层关系」和「账本核对」。关系层包括通信、当期已履约雇佣、贷款本金和证券持仓。每层超过 64 条时，界面按来源与目标 ID 显示前 64 条并标明总量与选取方式。账户列表按每页 64 个账户读取，附证券份额和贷款负债。
3. 选择一个主体，检查其原始授权观察、判断、引用证据、执行意图、回执及期末账户。使用「生成主体只读链接」另开观察者页。全局期次、关系和对照由研究者 Bearer 控制。观察者只能读授权运行、授权主体与已留存的 OBSERVE 输入。第 0 期无归档，拒绝跨主体、全局导出和写入。HTTP 绑定 `127.0.0.1`，普通工作台固定规则和零 API。
4. 展开「第 101 根 · 冻结冲击对照」。对照取阶段 05 的两条权重 2 臂，核对协议、配置、源码、原归档终态与同根期初状态。两个轨迹和「有冲击减无冲击」均从提交数据计算。展示数据来源、随机根、代码提交、源码哈希、各期提交及运行状态，支持 JSON 下载。

浏览器实际完成上述操作，选中 `household:0000` 后，研究者看到了授权估值和提交后的决策，观察者页只出现该主体的留存观察。第 3 期对照导出消费差 −6 件、企业销售额差 −66、证券标价差 −90，缺少当期可比成交价记 `null`。浏览器下载与原归档复算的 JSON 逐字节匹配，SHA-256 为 `6c3e756cc599dabbf2b7f867c4ef942ce52f3371acb5f85f5b29ab211cc21223`。桌面及 390 CSS px 移动视口均实测，窄屏表格在容器内横向滚动。证据位于 `build/convergence/06/browser-comparison.json`、`workbench-desktop.png` 和 `workbench-mobile.png`。

## 安装、回放与 CI

- 在仓库外新建 Python 3.12.13 虚拟环境，实际执行 `pip install .`、安装现有测试 extras 与 `pip check`。从仓库外分别导入 editable 与 wheel，核实模块导入位置、源码哈希和三项前端资产的 SHA-256。两种安装的源码和资产内容逐项匹配，wheel 在无 Git 检出的环境中诚实保留空代码提交元数据。
- wheel 独立执行第 2 期 checkpoint、续跑至第 5 期和全量独立执行回放，5 期均匹配，无储存增量注入。workbench 源码运行与 wheel 的初态及 5 个提交状态哈希全部相同，终态为 `23a7b11630edf2fbff29d5bb83c469a6f47e926964672b774f7342f7ddec49da`。外部调用为 0。证据位于 `build/convergence/06/wheel-equivalence/`。
- [CI 工作流](../../.github/workflows/quality.yml) 在 push、pull request 或手动触发时以 Python 3.12 运行，权限限于读仓库，清空模型凭据，执行安装、工作台资产与第三方 notice 检查及主要 pytest。仓库外干净环境执行了同一工作流的 shell 步骤，**97 项测试与 42 个子测试通过，0 失败、0 跳过**。pytest 用时 139.35 秒，含安装的总墙钟为 228.30 秒。日志见 `build/convergence/06/local-workflow.log`。GitHub 托管运行等待本地工作流进入远端，当前阶段无远端运行收据。
- wheel 带原有 `THIRD_PARTY_NOTICES.md` 和 EconSimulacra、Manyworld、AI Economist、ipfn 四份许可文本。根仓库仍无 `LICENSE`。第三方授权依各自条款，源码公开和构建产物均不授予 Laputa 自有代码的通用开源复制、修改或再分发权限。[GitHub 许可说明](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository)

## 规则负载粗测

Darwin arm64、Python 3.12.13、单进程，保留观察归档、2 家银行、财政和 4 家企业。使用现有 C/D 商品与机构规则路径。证券反馈配置的合同范围为 8–16 户，尺度测试沿用该合同限制。结果含归档落盘和投影，单次墙钟为 `/usr/bin/time -l` 实测，内存为最大 RSS，输出为归档普通文件逻辑字节和。

| 负载 | 已提交 | 墙钟 | 峰值 RSS | 归档输出 | 退出码 / API 调用 |
| --- | ---: | ---: | ---: | ---: | --- |
| 32 户 × 20 期 | 20 | 14.80 s | 410.92 MiB | 14.56 MiB | 0 / 0 |
| 100 户 × 20 期 | 20 | 51.63 s | 1,072.95 MiB | 43.91 MiB | 0 / 0 |

初版配置将企业数提至 4 家时保留 10 单位初始价差，两个进程均在配置校验处以状态 1 退出。确认报价上限后把粗测的企业报价差固定为 0，两项均运行完成。配置、初版失败日志、完成收据与计时均保存在 `build/convergence/06/performance/`。这些单次测量用于记录本机成本。**双根 1,000 户 × 100 期的正式性能与回放门为 PENDING。**
