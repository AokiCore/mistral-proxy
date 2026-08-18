# -*- coding: utf-8 -*-
"""上游调用执行器：账号挑选、并发闸门、429/5xx 故障转移、资源归还。

chat / embeddings / moderations / models 都走这里，避免每个端点各写一份重试逻辑。

资源所有权是这个模块最容易出错的地方，规则写死为：
  open() 成功返回 Lease 后，账号 inflight、并发信号量、上游响应三者的所有权都转移给 Lease，
  调用方必须保证最终调用一次 lease.aclose()（流式场景由生成器的 finally 负责）。
  open() 抛异常时，这三者已在内部释放干净。
"""
import asyncio
import time
from dataclasses import dataclass, field

import httpx

from core.billing import next_reset_ts
from core.config import API_BASE, UA


class UpstreamFailure(Exception):
    """所有账号都试过仍未成功。"""

    def __init__(self, message: str, status: int = 429, retry_after: float = 0.0,
                 attempts: int = 0):
        super().__init__(message)
        self.message = message
        self.status = status
        self.retry_after = retry_after
        self.attempts = attempts


class UpstreamRejected(Exception):
    """上游明确拒绝且换账号也没用（4xx，通常是请求本身的问题）。"""

    def __init__(self, status: int, body: bytes, account_email: str, attempts: int = 1):
        super().__init__(f"upstream rejected with {status}")
        self.status = status
        self.body = body
        self.account_email = account_email
        self.attempts = attempts


@dataclass
class Lease:
    response: httpx.Response
    account: object  # 实际是 Org
    attempts: int = 1
    started_at: float = field(default_factory=time.time)
    reserved: int = 0
    _pool: object = None
    _sem: asyncio.Semaphore | None = None
    _closed: bool = False

    @property
    def account_email(self) -> str:
        return getattr(self.account, "email", "?")

    async def read(self) -> bytes:
        return await self.response.aread()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.response.aclose()
        except Exception:
            pass
        if self._pool is not None:
            self._pool.release(self.account, self.reserved)
        if self._sem is not None:
            self._sem.release()


class Upstream:
    def __init__(self, pool, client: httpx.AsyncClient, sem: asyncio.Semaphore, settings):
        self.pool = pool
        self.client = client
        self.sem = sem
        self.settings = settings

    async def open(self, method: str, path: str, *, json_body=None, params=None,
                   est_tokens: int = 0, stream: bool = False,
                   on_attempt_failed=None, skip_limits: bool = False) -> Lease:
        """挑账号发请求，直到拿到 2xx 或重试次数耗尽。

        on_attempt_failed(account, status, body, error) 用于让调用方记录每次失败的尝试。
        skip_limits=True 时跳过 update_limits（conversations 端点的限速头与 chat 不同，
        不应覆盖 chat 的限速快照），且 pick 时不检查 limit_req。
        """
        attempts = 0
        last_status, last_error = 0, "unknown"

        for _ in range(max(1, self.settings.max_retry_accounts)):
            account = self.pool.pick(est_tokens, ignore_req_limit=skip_limits)
            if account is None:
                wait = self.pool.next_window_wait()
                raise UpstreamFailure(
                    "Rate limit exceeded: every account in the pool is cooling down",
                    429, wait, attempts)

            attempts += 1
            acc_held, sem_held, response = True, False, None
            try:
                await self.sem.acquire()
                sem_held = True

                headers = {"Authorization": f"Bearer {account.api_key}",
                           "User-Agent": UA,
                           "Accept": "text/event-stream" if stream else "application/json"}
                if json_body is not None:
                    headers["Content-Type"] = "application/json"

                request = self.client.build_request(
                    method, f"{API_BASE}{path}", json=json_body, params=params,
                    headers=headers)
                try:
                    response = await self.client.send(request, stream=True)
                except httpx.HTTPError as e:
                    last_error = f"{type(e).__name__}: {e}"
                    last_status = 0
                    self.pool.mark_error(account, 0, last_error)
                    if on_attempt_failed:
                        on_attempt_failed(account, 0, b"", last_error)
                    continue

                if not skip_limits:
                    self.pool.update_limits(account, response.headers)
                status = response.status_code

                # 402 = 该账号的月度美元额度花光了，换个号还有救，所以和 429/5xx
                # 一样走故障转移，而不是当成请求本身的问题直接抛给客户端。
                if status == 402:
                    body = await response.aread()
                    await response.aclose()
                    response = None
                    self.pool.mark_exhausted(account, next_reset_ts())
                    last_status, last_error = status, body[:200].decode("utf-8", "replace")
                    if on_attempt_failed:
                        on_attempt_failed(account, status, body, last_error)
                    continue

                # 401 = API key 失效（被删/过期），换号重试，并长时间冷却避免反复试。
                if status == 401:
                    body = await response.aread()
                    await response.aclose()
                    response = None
                    self.pool.mark_error(account, status, retry_after=86400.0)
                    last_status, last_error = status, body[:200].decode("utf-8", "replace")
                    if on_attempt_failed:
                        on_attempt_failed(account, status, body, last_error)
                    continue

                if status == 429 or status >= 500:
                    retry_after = _retry_after(response.headers)
                    body = await response.aread()
                    await response.aclose()
                    response = None
                    self.pool.mark_error(account, status, retry_after=retry_after)
                    last_status, last_error = status, body[:200].decode("utf-8", "replace")
                    if on_attempt_failed:
                        on_attempt_failed(account, status, body, last_error)
                    continue

                if status >= 400:
                    body = await response.aread()
                    await response.aclose()
                    response = None
                    raise UpstreamRejected(status, body, account.email, attempts)

                self.pool.mark_success(account)
                lease = Lease(response=response, account=account, attempts=attempts,
                              reserved=est_tokens, _pool=self.pool, _sem=self.sem)
                response, sem_held, acc_held = None, False, False
                return lease
            finally:
                if response is not None:
                    await response.aclose()
                if sem_held:
                    self.sem.release()
                if acc_held:
                    self.pool.release(account, est_tokens)

        raise UpstreamFailure(
            f"All {attempts} account attempts failed (last: {last_status} {last_error})",
            429 if last_status in (0, 429) else last_status or 502, 0.0, attempts)

    async def request_json(self, method: str, path: str, **kw) -> tuple[bytes, Lease]:
        """非流式便捷封装：读完 body 并归还资源，只返回内容与元信息。"""
        lease = await self.open(method, path, **kw)
        try:
            return await lease.read(), lease
        finally:
            await lease.aclose()


def _retry_after(headers) -> float:
    raw = headers.get("Retry-After")
    if not raw:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0
