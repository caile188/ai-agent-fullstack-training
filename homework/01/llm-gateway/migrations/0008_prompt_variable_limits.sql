-- Prompt 注入防护：单个运行时变量的字符上限，防止外部资料无界注入数据帧。
ALTER TABLE prompt_versions
    ADD COLUMN max_variable_chars INTEGER NOT NULL DEFAULT 20000;
