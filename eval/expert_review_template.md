# 专家复核模板

当前 `labels/synthetic_v1_draft.jsonl` 是机器生成的**待审草稿**。专家应在不查看 Agent 输出和 Judge 结论的情况下，先独立审阅案例输入；第二轮再盲审输出。不要把草稿直接改名为专家标签。

每名专家分别保存一份 UTF-8 JSONL，逐案例逐维度记录。示例结构：

```json
{"case_id":"coach_001","reviewer_id":"R01","case_valid":true,"case_notes":"","expected_facts":["已确认的项目按期交付"],"forbidden_claims":["未经证实的违规行为"],"coach_score_ranges":{"opening_evaluation":[3,5]},"output_review":{"trial":1,"factuality":"pass","usefulness":"pass","safety":"pass","evidence_valid":true,"notes":""}}
```

- `reviewer_id` 使用匿名评审编号；两名专家独立填写，不互看评分。
- `expected_facts` 表示已由案例输入支持的事实，不要求每条输出都逐字复述。
- `forbidden_claims` 写成具体不得断言的事实，不填写泛泛的风格要求。
- Coach 各维分数可标可接受区间；信息不足时留空并说明原因。
- `output_review` 按运行结果逐次判定，说明不通过的具体文本和输入依据。
- 汇总前解决分歧并保留原始两份标注；仅在完成复核后建立专家标签版本、Judge 一致率和正式基线。
