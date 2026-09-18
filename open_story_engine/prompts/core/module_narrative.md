依照原著题材写一章中文互动小说，承接已确认剧情。只输出小说正文，不输出标题、状态或菜单。

{{ module_scope_text }}
本章唯一行动：{{ title }}。{{ summary }}
本章动作性质：{{ get }}
开场位置：{{ location_name }}。
结束位置：{{ current_location_name }}。写清这一行动怎样发生，到此结束，不提前执行下一方向。
视角：{{ perspective_instruction }}
玩家身份：{{ profile_text }}
在场角色资料：
{{ character_text }}
身份揭示的原文依据（同一人物的别称不能拆成两人）：
{{ state_json }}
人物位置约束：
{{ character_location_text }}

可以确认的事实：
{{ fact_text }}
本回合实际状态变化：{{ state_transition_json }}
分支状态账本（跨回合因果的唯一运行时依据）：{{ branch_ledger_text }}
连续性：{{ continuity_text }}

写作参考（只可作为原著背景。参考里未被上述状态确认的取物、开门、转移等动作，不能写成本分支已经发生）：
{{ source_scene_text }}

允许的地点：{{ location_text }}
已确认可操作的物品：{{ available_item_text }}
其他物品即使在原著出现，也不能拿取、交接、使用或变成新线索。无名乘客仅作安静背景。

硬边界：
{{ state_guardrail_text }}
{{ protected_character_text }}
没有登记的幕后人物、司机或调度员不能提供报告或指令；不能为通信增加新收件人、群发、转发、回拨成功或关机原因。对讲机可以发出已确认的命令，但不得创造对端回话。
没有登记的往事、证据、设备规则、暗道或新地点不能成为转折。人物可以怀疑、拒绝、犹豫、试探；不能通过对话宣布一个新事实已经被证实，也不能将普通环境现象擅自改成超自然事件。

叙事安排：先写环境压力和当下行动，再写在场人物围绕同一行动的具体交锋，最后落实本回合结果。让对话各有目的，减少反复问同一件事与重复雨声比喻；张力来自已知事实之间的冲突，不来自新消息或新线索。
{{ length_instruction }}
{{ terminal_instruction }}
只交付一份完整正文，不解释规则。{{ repair_instruction }}
