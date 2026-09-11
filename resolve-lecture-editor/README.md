# Resolve Lecture Editor

在 DaVinci Resolve 中处理课程、访谈和口播视频的剪辑与 AI B-roll 工作流。

这个 skill 接手已有的 Resolve 工程和目标时间线，完成内容精剪、视觉策划、AI B-roll 首尾帧确认、视频与同步音效生成、回填以及最终可编辑交付。默认采用 B 模式，主动压缩重复解释与同质例子；不会自动导出成片或启动整片渲染。

## 适用场景

- 课程、讲座、访谈和口播内容的结构化精剪
- 根据讲稿和时间线规划 B-roll
- 使用 Vox 纸质拼贴风格制作 AI 视频素材
- 生成并回填无音乐、无对白的同步音效
- 添加可编辑的 Resolve 原生标题
- 保留原始时间线并维护可恢复的工作版本

## 核心工作流

1. 接收并分析当前工程、时间线、轨道和素材。
2. 创建内容剪辑工作版本，压缩重复内容并等待剪辑确认。
3. 用 B001、T001 等范围标记规划 B-roll 和标题。
4. 为批准的镜头生成首帧、尾帧，先确认视觉风格。
5. 核对服务商官方报价，确认费用后生成样片；样片通过后并发提交剩余任务。
6. 将视频、同步音效和批准的标题回填到包装工作时间线。
7. 检查帧位置、音频链接、音量、轨道层级和时间线完整性，交付可继续编辑的工程。

## 目录结构

```text
resolve-lecture-editor/
├── SKILL.md
├── README.md
├── agents/openai.yaml
├── references/
│   ├── editing.md
│   ├── placement-and-titles.md
│   ├── planning-and-markers.md
│   ├── providers-and-pricing.md
│   ├── resolve-operations.md
│   ├── state-contract.md
│   └── visuals.md
└── scripts/
    ├── resolve_snapshot.py
    ├── test_workflow.py
    └── workflow.py
```

## 使用方式

在 Codex 中调用 `resolve-lecture-editor`，并说明要处理的 Resolve 工程或时间线，以及本次需要完成的阶段。例如：

> 使用 resolve-lecture-editor，分析当前 Resolve 时间线，先完成内容精剪并给我剪辑确认摘要。

如果已有运行目录，工作流会读取已有状态并继续；首次运行时，在用户工程素材目录下创建独立运行目录，并执行：

```bash
python3 scripts/workflow.py init <run_dir>
```

脚本只负责本地状态、帧数计算、报价校验、文件指纹和任务账本，不连接 Resolve、不调用付费 API，也不替代用户确认。

## 重要约定

- 原始时间线保留为恢复依据，内容剪辑和包装分别维护工作版本。
- 未经明确批准的 B-roll 不会进入生成和回填阶段。
- 付费生成前必须提供官方报价依据并获得明确确认。
- 未知提交结果不会自动重发；失败的付费任务不会自动重试。
- 包装阶段不 ripple，不改变原讲授音量、剪辑、字幕和全片时长。
- 默认不创建字幕轨、不导出成片、不启动整片渲染。
- 中英双语字幕使用同一条字幕轨中的两个独立 Subtitle Regions。

## 相关文档

- [技能主说明](SKILL.md)
- [剪辑操作](references/editing.md)
- [Resolve 操作与版本管理](references/resolve-operations.md)
- [B-roll 标记规划](references/planning-and-markers.md)
- [首尾帧与视觉规范](references/visuals.md)
- [服务商与报价](references/providers-and-pricing.md)
- [回填与标题](references/placement-and-titles.md)

