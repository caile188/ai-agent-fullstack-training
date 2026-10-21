-- Prompt 合约规则/数据分层：运行时变量只允许出现在独立的 user_template，
-- system_template 保持纯静态规则。
ALTER TABLE prompt_versions ADD COLUMN user_template TEXT NOT NULL DEFAULT '';
