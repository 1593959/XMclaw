# AGENTS.md - 代理集群配置

## 当前代理

| 代理名 | 描述 | 状态 |
|--------|------|------|
| default | 默认主代理 | active |

## 委派规则

- 当任务涉及特定领域时，default 代理可委派给专门的子代理
- 子代理负责独立完成任务后汇报结果
- 多代理协作时，通过事件总线协调

## 未来扩展

> 当需要多代理时，在此定义团队配置：
> 
> ```yaml
> teams:
>   - name: research_team
>     agents:
>       - search_agent
>       - analysis_agent
>       - report_agent
> ```

