将玩家的中文自由文本判定为当前一个已公布方向，或要求澄清/拒绝。不可发明方向，不可把多个目标合为一步；若多个目标并列，选择文本中最先明确的一个。拒绝超自然、瞬移、复活等违背世界设定的内容。
当前方向：{{ directions_json }}
玩家输入：{{ player_direction }}
只输出 {"kind":"accepted","directionId":"已公布ID","rationale":""}，或 {"kind":"clarification_needed","message":""}，或 {"kind":"rejected","message":"","citations":[{"kind":"immutable_fact","ref":"fact_no_supernatural"}]}。