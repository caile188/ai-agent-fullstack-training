PYTHON ?= python
VENV   ?= .venv
BIN    := $(VENV)/bin
IMAGE  ?= governed-llm-gateway:latest

.PHONY: help venv install dev run test docker-build docker-run smoke clean

help: ## 列出可用目标
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

venv: ## 创建虚拟环境
	$(PYTHON) -m venv $(VENV)

install: venv ## 安装运行依赖
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install .

dev: venv ## 安装运行 + 测试依赖
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install ".[dev]"

run: ## 本地启动（需先 export DEEPSEEK_API_KEY）
	$(BIN)/uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload

test: ## 运行全部单测（MockTransport，不触真实网络）
	$(BIN)/python -m pytest -q

docker-build: ## 构建容器镜像
	docker build -t $(IMAGE) .

docker-run: ## 运行容器（读取 .env；持久化 ./data）
	docker run --rm -p 8080:8080 --env-file .env \
		-v "$(PWD)/data:/app/data" $(IMAGE)

smoke: ## 对运行中的网关跑端到端冒烟（默认 http://127.0.0.1:8080）
	$(BIN)/python scripts/smoke.py

clean: ## 清理缓存与本地产物
	rm -rf .pytest_cache **/__pycache__ **/**/__pycache__
