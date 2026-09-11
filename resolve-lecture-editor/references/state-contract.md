# 运行状态与本地工具

所有 JSON UTF-8。运行数据在独立 run_dir，不写进 skill；凭证不进入任何文件。路径用绝对路径。读现有记录再恢复，不能 init 覆盖。workflow.py 使用标准库，无第三方包和网络请求。

## 文件职责

| 文件 | 内容 |
|---|---|
| state.json | run_id、阶段、默认模式B、原/剪辑/包装时间线ID、确认记录及检查结果 |
| original_snapshot.json / edit_snapshot.json / package_snapshot.json | 真实回读结构及最后写入状态；保存既有用户变化 |
| transcript.json / edit_map.json | 原始转写、源时间码原点、源到剪后区间的可追溯关系及删除说明 |
| plan.json | fps、时间线边界、B/T区间、原文和标记反馈 |
| style.json | 独立可扩展风格配置；默认vox-paper-v1 |
| generation.json | 已批准镜头与生成内容，不绑定后期音量/落点 |
| provider.json / quote.json | 当前服务协议及官方报价证据 |
| approval.json / pilot_approval.json | 用户真实确认记录、报价与样片指纹 |
| jobs.sqlite | 原子提交意图、任务ID与状态；多个并发任务可安全写入 |
| placement.json | 本技能拥有的媒体/轨道/项目ID、实际范围、音量和最后回读值 |

代理维护阶段与内容确认：analysis→editing→edit_approved→planning→plan_approved→frames→frames_approved→quoted→cost_approved→pilot→pilot_approved→generating→placement→review→complete。这些阶段不由脚本假装自动判定；相应确认保存用户原话/消息引用、时间、批准对象及其hash。只有用户内容验收后才 complete。标题样式批准单独记录，可缺省。

## 帧几何

plan.json 必需 fps（如30000/1001）、start_frame、end_frame_exclusive、items。每项 id、kind=broll或title、in_frame、out_frame；可选lane，默认按kind分组。区间为整数绝对 [in,out)。同lane不能重叠，B/T可互相覆盖。原API端点用 end_frame_raw 保存，必须验证后才能解释成半开区间，不能直接重命名为exclusive。

`python3 scripts/workflow.py plan <plan.json>` 检查范围、重复编号与同lane冲突。它不读取真实轨道，最终仍须Resolve检查。

可导入工具函数：fps、tc_frames、frame_window。tc_frames返回编号对应的帧数，调用方显式传drop和源时间码原点；frame_window用于从已校准的相对毫秒得到绝对范围/相对标记位置，起止分别取整。不是对变速或嵌套转写的通用映射器。

## generation.json

根shots数组，每镜必需：

- id；plan_status/start_status/end_status均为approved；保存真实确认依据到plan/state，不凭填字段制造批准。
- start_path/end_path；start_prompt/end_prompt/motion_prompt/sfx_prompt；style_hash。
- audio_strategy=native_sfx。
- generation_parameters：实际生成时长、画幅、分辨率与其他确定的生成输入，不能只写“默认”。提供商专属精确请求另在quote记录。

`python3 scripts/workflow.py fingerprint <run_dir>` 输出 generation_hash，按实际图片字节及提示词/生成参数计算。仅移动剪辑落点或调后期增益不改此hash；改图、动作、生成参数会改变。批准后不覆盖生成文件，保留版本。

## quote.json 必需字段

- quote_id（每次新报价唯一）、provider、currency（三字母ISO，同报价一种币种）。
- checked_at、valid_until（带时区ISO时间）、validity_basis（官方有效期或本次执行窗口依据）。不能伪称自行设置窗口是官方保证。到期仅阻止新POST，不阻止已提交任务查询/下载。
- official_source_verified=true、pricing_complete=true：只有代理真实核查官方资料且无缺项后填写。脚本不替代理判定真实性。
- generation_hash、pilot_id、total（十进制字符串）。
- evidence：以证据ID索引对象，每项url为官方HTTPS链接、checked_at、basis为简明计费/能力依据。原始官方响应可另存本地并记录hash。
- jobs：与generation镜头一一对应，每项shot_id、model、request_parameters（完整不含秘钥）、requested_seconds（十进制字符串）、rounding_rule、billing_scope（税费/音频/分辨率/质量等是否已含及依据）、first_frame_field、last_frame_field、native_sfx_evidence（对应证据ID）、subtotal。
- jobs[].lines：每项unit、quantity、quantity_basis（如何舍入/计算）、unit_price、certainty=fixed/estimated/metered、evidence_id。quantity/unit_price/subtotal/total均十进制字符串，不传浮点。

校验计算每条quantity×unit_price、逐镜subtotal与total。若服务商采用阶梯、最低收费或额外舍入，按官方规则拆成明确收费行；不能在未解释的算术差异中隐藏差价。未知费用不填0。费率确定但数量取决实际输出时标estimated/metered；总额以估计展示，不当固定承诺。

运行 `python3 scripts/workflow.py quote <run_dir>` 得到样片/剩余/总额。报价检查不是价格抓取器；官方访问由代理通过浏览或已配置服务工具完成。

## 批准和任务命令

真实用户确认费用后：

    python3 scripts/workflow.py approve <run_dir> --confirmation '用户确认原话或消息引用'

每个POST前（先对样片）：

    python3 scripts/workflow.py claim <run_dir> --shot B001

claim是本地原子占位，不会发送请求。占位成功后由服务适配执行一次POST；占位失败先检查账本。返回后立即记录：

    python3 scripts/workflow.py job <run_dir> --quote-id <id> --shot B001 --state submitted --job-id <实际ID> --detail '实际响应摘要'

用job依次记录 completed→downloaded→ready，detail包含真实输出URL/文件路径与技术检查结果。失败按实际阶段记 failed、unknown_submission、download_failed或conform_failed。job更新允许报价过期，以便恢复已付费任务。failed没有到submitting的路径。未知任务只能经提供商核实转submitted/failed，不能重POST。

样片已ready且用户真实批准后：

    python3 scripts/workflow.py approve-pilot <run_dir> --path <样片绝对路径> --confirmation '用户确认原话或消息引用'

保存实际文件hash与job ID，允许其余镜头claim。代理一次性调度全部剩余任务；账本不自带HTTP执行器。`status <run_dir>`查看所有报价尝试。新付费重试采用新quote_id，保留旧账本；将旧quote/generation/approval归档后才替换当前文件。只重试必要镜头，不重收其他镜头费用。

本地确认字段是审计记录，不是防恶意用户的权限系统。任何脚本都不能凭写入approved替代会话授权。
