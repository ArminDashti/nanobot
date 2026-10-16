# 实例 home、config 与 agent workspace 路径契约

状态：提案。本文定义目标行为，尚未由当前运行时完整实现。当前使用方式见[多实例文档](../docs/multiple-instances.md)。

跟踪：[NAN-264](https://linear.app/nanobot-ai/issue/NAN-264/)。

一个规范化后的 home 目录对应一个 nanobot 实例。`--config` 选择该实例本次使用的配置文件，`--workspace` 覆盖该实例本次使用的 agent workspace。三个参数保留，各自只承担一种职责。

本规范先确定实例身份、路径解析和数据归属。实例命名、生命周期命令重命名，以及 systemd、launchd 和 Windows 服务适配另行设计。[NAN-36](https://linear.app/nanobot-ai/issue/NAN-36/) 中以配置路径定义实例身份的方案，需要以本规范的 home 身份为基础调整。

## 术语与边界

| 名称 | 含义 |
|---|---|
| home | 实例身份和 nanobot 自有持久状态的根目录。记为 `H`。 |
| config | 本次运行加载的配置文件，可以位于 home 外，也可以使用其他文件名。记为 `C`。 |
| agent workspace | `agents.defaults.workspace` 指向的 agent 长期资料目录，保存记忆、技能、`SOUL.md`、`USER.md`、心跳任务、cron 任务和 workspace 身份标记。记为 `W`。 |
| 项目目录 | 某个会话选择的工具工作目录。项目目录不参与实例身份、config 或 agent workspace 的解析。 |
| 启动目录 | 调用命令时的进程工作目录，只用于解释命令行中的相对路径。 |

本文中的 `workspace` 和 `--workspace` 始终指 agent workspace。WebUI 中选择项目不修改 `W`。

未选择独立项目时，agent workspace 可以兼作文件工具和 shell 的默认工作目录。选择项目后，工具默认相对路径和 shell 工作目录使用项目目录，agent 资料、记忆与技能仍来自 `W`。工具显式工作目录和访问限制继续由现有工具策略处理。解析 `W` 不要求改变 nanobot 进程的工作目录。

当前 CLI 没有独立的 `--project` 参数。原生 TUI 默认用启动目录选择初始项目，现有 `--workspace` 还混合影响该项目和 agent workspace。实现本契约时必须拆开这两个输入。新增项目参数的名称和操作方式不由本规范决定。

## 实例身份

规范化后的 `H` 是实例身份。config 文件名、config 内容、`W`、会话项目目录和启动方式均不产生第二个实例身份。

同一 home 下的两个配置文件是同一实例的可选配置。它们共享认证、配对、渠道登录状态等实例数据。两个文件位于同一父目录，并通过 `--config` 单独启动时，也属于同一实例。

创建独立实例需要使用不同的 home。独立 agent 还需要不同的 agent workspace，避免共享记忆与技能。目录分离不提供操作系统级隔离，现有工具权限和沙箱边界继续生效。

本契约不增加 `instanceHome` 配置字段，也不增加实例描述文件。home 由启动入口解析，配置文件不能在加载后重新定义 home。

## 路径解析顺序

解析器先规范化显式命令行路径，再确定 `H` 和 `C`，最后加载配置并确定 `W`。显式路径为空时报告参数错误，不把空字符串解释为当前目录。

### home 与 config

| 显式参数 | 有效 home `H` | 配置文件 `C` |
|---|---|---|
| 都不指定 | `~/.nanobot` | `H/config.json` |
| 只有 `--home H` | 指定的 home | `H/config.json` |
| 只有 `--config C` | 指定配置文件的规范化父目录 | 指定的配置文件 |
| 同时指定 `--home H` 和 `--config C` | 指定的 home | 指定的配置文件 |

`--home` 与 `--config` 可以组合。显式 home 的优先级高于从 config 父目录推导的 home。

全局 `--home` 保持在子命令前，`--config` 和 `--workspace` 沿用各命令的参数入口：

```bash
nanobot --home ~/bots/work gateway --config /etc/nanobot/work.json --workspace ~/agents/work
```

例如，`--home ~/bots/work` 与 `--config /etc/nanobot/work.json` 组合时，配置来自 `/etc/nanobot/work.json`，实例数据仍归属于 `~/bots/work`。配置文件存在于 home 外，不会再把认证或运行状态目录改到配置文件旁边。

未显式指定 `--config` 时，程序从已确定的 `H/config.json` 加载配置。该文件是指向外部配置的符号链接时，home 仍是 `H`。

显式 `--config` 指向不存在或无效的配置时，普通运行命令报错，不回退到其他配置。初始化命令可以按其创建职责生成指定配置。

未显式指定 config 且 `H/config.json` 不存在时，各入口保留其初始化或配置提示职责，不搜索其他 home，也不改变已经解析的路径。

### agent workspace

`W` 按以下优先级选择来源，已指定但无效的来源报错：

1. 显式 `--workspace`。
2. 已加载配置中持久化的 `agents.defaults.workspace`。
3. `H/workspace`。

已持久化的 workspace 保持优先级。指定新 home 不会重写已有的绝对 workspace，也不会移动原 workspace 中的数据。

配置缺少 workspace 字段时，使用 `H/workspace`。加载器必须保留字段是否配置这一事实，不能先注入旧默认值 `~/.nanobot/workspace`，再把它当作持久化设置。

配置字段显式为空、类型错误或无法按路径规则解析时报告配置错误，不回退到默认目录。

配置中的 `${VAR}` 引用继续按现有配置规则解析，然后应用 workspace 的路径基准。引用解析不改变 home 或字段是否显式配置的事实。

### 相对路径与规范化

| 路径来源 | 相对路径的基准 |
|---|---|
| 命令行 `--home`、`--config`、`--workspace` | 启动目录 |
| 配置中的 `agents.defaults.workspace` | 有效 home `H` |
| nanobot 自有的默认状态路径 | 有效 home `H` |

展开 `~` 后，路径统一为绝对路径，并解析存在的符号链接或 Windows 目录连接。命令行路径基准在启动时固定，不受服务管理器后续工作目录或会话项目变化影响。

身份比较使用平台文件系统语义。相对路径别名、符号链接和目录连接指向同一 home 时，必须识别为同一实例。不能在所有平台上直接把路径转为小写，也不能仅以原始字符串区分实例。

显式 `--config` 单独使用时，从规范化后的配置文件位置推导 home。需要保留配置链接所在目录作为 home 时，显式传入 `--home`。

## workspace 覆盖与配置写入

普通启动命令的 `--workspace` 只覆盖本次运行的 `W`。它不修改配置文件，不改变 home，不自动搬迁 agent 资料、认证或会话。

初始化、配置编辑和明确的保存操作可以持久化 workspace。保存使用被选中的 `C`，不能另写到 `H/config.json`。读取外部只读配置时，普通启动不得仅因存在 `--workspace` 而尝试保存配置。

所有入口采用同一覆盖规则，包括 gateway、API、WebUI 启动器和终端 agent。原生 TUI 不能把 `--workspace` 同时当作项目选择参数。连接已有运行时的客户端也不能把一个本地 agent workspace 覆盖静默解释成会话项目选择。

## 数据归属

以下路径表示目录归属。各模块保留现有文件名、数据库格式和子目录结构，本规范不要求统一迁入新的 `state/` 目录。

| 数据 | 归属 |
|---|---|
| 配置和显式配置保存 | `C` |
| agent 资料、记忆、技能、心跳任务和 cron 任务 | `W` |
| workspace 身份标记 | `W/.nanobot/workspace-id` |
| 私有会话历史 | `H/sessions/<workspace-id>/` |
| nanobot 管理的 OAuth、MCP 认证、配对和渠道登录状态 | `H` 下各所属模块的存储 |
| 媒体、日志、WebUI 持久状态和 CLI 输入历史 | `H` 下各所属模块的存储 |
| PID、进程身份、锁和客户端租约 | `H` 下的易变运行状态 |

认证存储不能隐式回退到另一个用户级目录或其他软件的登录文件。需要复用已有认证时，通过明确的导入或登录操作完成。第三方 OAuth 库的默认目录、环境覆盖和自动导入行为必须在 nanobot 的存储适配边界统一处理。

已有渠道专属数据库路径等显式外部存储设置继续由对应模块拥有。它们不能重新定义 home，也不能成为其他模块的默认根目录。路径检查和备份清单必须列出这些外部依赖，不能宣称仅备份 home 就覆盖所有数据。各模块专属路径参数的完整重设计另行处理。

短期网关令牌等内存状态不纳入持久备份。PID、锁和租约等易变状态也不作为可恢复的历史数据复制。

### 会话与 workspace 身份

私有会话继续按 workspace 身份分区。home 变化会改变私有会话根目录，agent workspace 变化可能选择另一份会话分区。两种变化都不意味着旧会话已经被移动或删除。

保留现有 workspace 移动、复制检测与身份归属检查。移动 workspace 时保留 `workspace-id`，复制一个仍在使用的 workspace 时不得让两个副本意外写入同一会话分区。完整复制实例时，仍需按会话存储的归属规则导入历史。

继续禁止私有会话根目录位于配置的 agent workspace 内。home 路径或 workspace 覆盖导致重叠时，程序在启动和创建 agent 文件前报告错误。该检查不扩大为新的项目目录访问限制。

## 运行时与后续实例管理

所有入口先得到同一份已解析的 home、config 和 agent workspace，再把路径交给网关、会话、认证与渠道模块。检查多个实例不能通过反复修改进程级活动 home 或 config 完成。

派生的子进程必须接收已解析的路径。特别是外部 config 与显式 home 组合时，只转发 `--config` 会丢失 home，子进程不得按另一套默认值重新推导。

查询状态、停止、查看日志和运行时锁以 home 定位对应组件。运行记录保留实际使用的 config 和 agent workspace，供检查和配置冲突诊断。一个实例可以运行不同职责的组件，但同一 home 的同一组件不能因为换了 config 或 workspace 参数就重复启动。

目标组件已经运行，而启动请求指定了不同的 config 或 agent workspace 时，程序展示正在使用的路径并报告不一致，不静默忽略覆盖，也不再启动第二份。切换这些路径需要显式停止后启动。仅改变会话项目目录不触发此规则。

config 内容编辑不改变实例身份。具体配置项的热重载策略不由本规范决定。运行中的 home 和 agent workspace 不随配置保存静默切换。

## 当前行为与兼容影响

三个参数继续保留，但统一语义会改变已有的部分行为，不能称为完全兼容。

| 场景 | 当前行为 | 目标行为 |
|---|---|---|
| 只传 `--config C`，配置未写 workspace | 主要状态在 `C.parent`，默认 workspace 与 CLI 历史仍可能在 `~/.nanobot` | home 统一为 `C.parent`，缺省 workspace 与 CLI 历史跟随该 home |
| 同时传 `--home H --config C`，且目录不同 | 主要状态在 `C.parent`，部分默认路径跟随 `H` | nanobot 自有默认状态统一跟随 `H` |
| 配置已持久化绝对 workspace | 使用持久化路径 | 继续使用持久化路径 |
| 配置持久化相对 workspace | 可能相对于进程工作目录 | 相对于有效 home |
| `webui --workspace W` | 启动器可能保存 agent workspace | 普通启动只做本次覆盖 |
| 原生 TUI 的 `--workspace W` | 同时参与 agent workspace、初始项目和运行时定位 | 只覆盖 agent workspace |
| 部分第三方 OAuth 凭据 | 使用用户级共享存储或自动导入 | 使用实例存储，跨存储导入明确执行 |
| 相同 home 下的不同配置或 workspace 选择器 | 可能得到不同的运行时键并启动多个组件副本 | 同一组件由 home 定位并阻止重复启动 |

当前实现依据见 [home 默认值](../nanobot/config/home.py)、[配置路径与数据目录](../nanobot/config/paths.py)、[配置加载与 workspace 绑定](../nanobot/config/schema.py)、[运行参数覆盖](../nanobot/cli/runtime_config.py)、[运行时身份](../nanobot/gateway/runtime.py)、[WebUI 启动保存](../nanobot/cli/webui.py)和 [TUI 项目选择](../nanobot/cli/tui_launcher.py)。

### 迁移要求

实现发布前必须提供只读路径检查，展示参数来源、配置是否显式保存 workspace，以及旧、新规则下的 home、config、agent workspace 和关键状态目录。检查不能创建目录、导入令牌、写入 workspace 标记或启动运行时，也不能输出凭据内容。

迁移根据路径来源处理已有数据：

1. 已持久化的绝对 workspace 原样保留。
2. 省略 workspace 或使用相对 workspace 的已有配置，先展示旧解析结果。保留原数据位置时，通过明确的配置修改固定原路径。
3. home 与 config 分离的部署，停止使用旧存储的相关写入进程，再备份并明确选择迁移数据或继续指定原 home。
4. 位于用户级目录的认证通过显式导入或重新登录接入实例，不在首次启动时自动复制。
5. 旧运行时记录和服务启动参数完成核对后，才能按新 home 身份启动，避免新旧标识同时管理同一个写入进程。

普通启动不执行数据搬迁。会改变数据查找位置的升级需要先完成路径检查和明确的迁移选择，不能静默启用新默认值后创建一套看似空白的实例。配置文件位置本身不承担数据布局版本标记的职责。

备份和迁移清单包含 `C`、home 下的持久状态、`W` 及模块显式外部存储。整体移动 home 会改变实例的路径身份，旧运行时必须停止，服务配置和实例索引需要更新。会话历史能否延续仍由 workspace 标记与私有存储共同决定。

本规范不引入会话数据库格式迁移，也不延长或缩短已有历史导入器的兼容窗口。正式实现需要在发布说明中列出上述行为变化和迁移入口。

## 验收场景

以下是实现阶段必须验证的外部行为，不表示本次规范提交已经实现这些行为。

| 场景 | 预期结果 |
|---|---|
| 无路径参数，配置未写 workspace | `H=~/.nanobot`，`C=H/config.json`，`W=H/workspace` |
| 仅指定 home，配置未写 workspace | config、缺省 workspace 和默认实例状态均跟随该 home |
| 仅指定 config，配置未写 workspace | home 为 config 的规范化父目录，缺省 workspace 和 CLI 历史随 home 变化 |
| 同时指定外部 config 与 home | config 从外部读取，默认实例状态位于指定 home |
| 配置已写 workspace，再指定 home | 使用持久化 workspace，不自动重写或搬迁 |
| 指定 workspace 覆盖 | 只替换本次 agent workspace，不改变 home，也不保存覆盖值 |
| 配置中的相对 workspace，从不同启动目录运行 | 解析到同一个 agent workspace |
| 命令行中的相对 workspace | 以该次启动目录解析为绝对路径 |
| 同一 home 的不同配置文件 | 共享实例身份和认证存储，组件启动冲突可以解释 |
| 不同 home，使用同一只读外部 config | 实例状态分别隔离，配置不被启动过程修改，agent workspace 仍按配置解析 |
| 同一 home 的不同路径别名 | 解析为同一身份，不重复启动同一组件 |
| workspace 覆盖变化 | 会话根仍在 home，现有 workspace 会话分区规则保持生效 |
| 选择会话项目 | 工具工作目录变化，home、config、agent workspace、记忆和技能归属不变 |
| 原生 TUI 使用 workspace 覆盖 | agent workspace 与项目输入不再复用同一个参数值 |
| 使用实例认证时，用户级认证文件同时存在 | 不隐式读取或导入用户级文件 |
| 私有会话根位于 agent workspace 内 | 启动前报错，不先创建 agent 文件 |
| 显式 config 缺失或无效 | 普通运行报错，不切换到其他配置 |
| 只读路径检查 | 解析并展示来源，不写文件、不启动进程、不导入认证 |
| 旧、新路径规则有差异的升级 | 先展示迁移选择，数据查找位置不静默改变 |
| 网关子进程启动或服务重启 | 保留同一组已解析路径，不因服务工作目录不同而改变归属 |

路径解析、来源与覆盖规则必须覆盖 Linux、macOS 和 Windows。Windows 场景包含带空格的路径、盘符与 UNC 路径、目录连接，以及适用文件系统上的大小写别名。涉及真实进程或文件系统身份的行为，需要在对应平台验证，不能只凭 POSIX 字符串模拟认定通过。
