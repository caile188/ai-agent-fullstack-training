"""服务装配与依赖注入（应用级单例）。"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import GatewayConfig, load_config
from app.core.rate_limit import RateLimiter
from app.services.db.connection import Database
from app.services.db.repositories import Repository
from app.services.eval_runner import EvalRunner
from app.services.execution.concurrency import ConcurrencyLimiter
from app.services.execution.engine import ExecutionEngine
from app.services.execution.health import CircuitBreaker
from app.services.gateway import GatewayService
from app.services.prompts_store import PromptStore
from app.services.registry import AdapterRegistry


@dataclass
class Services:
    config: GatewayConfig
    db: Database
    repo: Repository
    registry: AdapterRegistry
    breaker: CircuitBreaker
    limiter: ConcurrencyLimiter
    rate_limiter: RateLimiter
    engine: ExecutionEngine
    prompts: PromptStore
    gateway: GatewayService
    evaluator: EvalRunner


def build_services(config_path: str = "gateway.yaml") -> Services:
    config = load_config(config_path)
    db = Database(config.db_path)
    repo = Repository(db)
    registry = AdapterRegistry(config)
    breaker = CircuitBreaker(config.resilience.circuit)
    limiter = ConcurrencyLimiter(config)
    rate_limiter = RateLimiter(config)
    engine = ExecutionEngine(config, registry, breaker, limiter)
    prompts = PromptStore(repo)
    gateway = GatewayService(config, repo, registry, engine, breaker, limiter, prompts)
    evaluator = EvalRunner(gateway, prompts, repo)
    return Services(
        config=config, db=db, repo=repo, registry=registry, breaker=breaker,
        limiter=limiter, rate_limiter=rate_limiter, engine=engine, prompts=prompts,
        gateway=gateway, evaluator=evaluator,
    )
