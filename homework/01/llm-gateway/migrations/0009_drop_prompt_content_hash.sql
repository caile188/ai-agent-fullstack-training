-- 去掉合约侧 content_hash（及其 UNIQUE 约束）：系统只保留一个 hash，
-- 即每次调用渲染后的实发内容指纹（落 traces.prompt_hash）。
-- SQLite 不能直接删除被 UNIQUE 约束引用的列，按 12 步表重建方式处理。
CREATE TABLE prompt_versions_new (
    id                    INTEGER PRIMARY KEY,
    package_id            INTEGER NOT NULL REFERENCES prompt_packages(id),
    version               TEXT NOT NULL,
    status                TEXT NOT NULL CHECK (status IN ('draft','published','deprecated')),
    system_template       TEXT NOT NULL,
    user_template         TEXT NOT NULL DEFAULT '',
    variables_schema      TEXT NOT NULL DEFAULT '{}',
    few_shot_json         TEXT NOT NULL DEFAULT '[]',
    output_schema         TEXT,
    tool_versions         TEXT NOT NULL DEFAULT '[]',
    default_logical_model TEXT,
    generation_params     TEXT NOT NULL DEFAULT '{}',
    context_budget        INTEGER,
    max_variable_chars    INTEGER NOT NULL DEFAULT 20000,
    eval_set_json         TEXT NOT NULL DEFAULT '[]',
    eval_threshold        REAL,
    changelog             TEXT NOT NULL DEFAULT '',
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (package_id, version)
);

INSERT INTO prompt_versions_new
    (id, package_id, version, status, system_template, user_template,
     variables_schema, few_shot_json, output_schema, tool_versions,
     default_logical_model, generation_params, context_budget, max_variable_chars,
     eval_set_json, eval_threshold, changelog, created_at)
SELECT id, package_id, version, status, system_template,
       COALESCE(user_template, ''), variables_schema, few_shot_json, output_schema,
       tool_versions, default_logical_model, generation_params, context_budget,
       COALESCE(max_variable_chars, 20000), eval_set_json, eval_threshold,
       changelog, created_at
FROM prompt_versions;

DROP TABLE prompt_versions;
ALTER TABLE prompt_versions_new RENAME TO prompt_versions;
