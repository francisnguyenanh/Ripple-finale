"""
Vertex AI Gemini REST Client — Standalone & Reusable
=====================================================
Không phụ thuộc vào bất kỳ framework nào (Flask, Django...).
Chỉ cần: google-auth, Python 3.11+

Cách dùng trong dự án khác:
    1. Copy thư mục ai_module/ vào root project
    2. Đặt service account JSON vào ai_module/credentials/vertex/
    3. from ai_module import GeminiClient

Tham khảo cấu hình: ai_module/config/vertex.config.example.ini
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Generator

import google.auth
import google.auth.transport.requests

# ---------------------------------------------------------------------------
# Vertex AI REST endpoint template / builder (supports global and regional)
# ---------------------------------------------------------------------------
def build_vertex_endpoint(location: str, project: str, model: str, stream: bool = False) -> str:
    action = "streamGenerateContent" if stream else "generateContent"
    loc = location.strip().lower()
    if loc == "global":
        host = "aiplatform.googleapis.com"
    else:
        host = f"{loc}-aiplatform.googleapis.com"
    return f"https://{host}/v1/projects/{project}/locations/{loc}/publishers/google/models/{model}:{action}"

_ENDPOINT_TMPL = (
    "https://{region}-aiplatform.googleapis.com/v1/projects/{project}/"
    "locations/{region}/publishers/google/models/{model}:generateContent"
)

_STREAM_ENDPOINT_TMPL = (
    "https://{region}-aiplatform.googleapis.com/v1/projects/{project}/"
    "locations/{region}/publishers/google/models/{model}:streamGenerateContent"
)


# ---------------------------------------------------------------------------
# ChatSession — multi-turn conversation
# ---------------------------------------------------------------------------

class ChatSession:
    """
    Multi-turn chat session với Vertex AI Gemini.
    Lưu history theo định dạng Vertex AI contents[].
    Có thể serialize history (list of dict) vào Flask session / Redis / JSON.

    Ví dụ:
        chat = client.start_chat()
        reply = chat.send_message("Xin chào!")
        # Lưu session:
        saved = chat.export_history()
        # Khôi phục:
        chat2 = client.start_chat()
        chat2.import_history(saved)
    """

    def __init__(self, client: "GeminiClient") -> None:
        self._client = client
        # Định dạng: [{"role": "user"|"model", "parts": [{"text": "..."}]}]
        self._history: list[dict] = []

    def send_message(self, text: str) -> str:
        """Gửi tin nhắn, cập nhật history, trả về reply text."""
        self._history.append({"role": "user", "parts": [{"text": text}]})
        reply = self._client._call(self._history)
        self._history.append({"role": "model", "parts": [{"text": reply}]})
        return reply

    def stream_send_message(self, text: str) -> "Generator[str, None, None]":
        """Stream reply tokens; cập nhật history sau khi nhận đủ response."""
        self._history.append({"role": "user", "parts": [{"text": text}]})
        full_reply = ""
        for chunk in self._client._stream_call(self._history):
            full_reply += chunk
            yield chunk
        self._history.append({"role": "model", "parts": [{"text": full_reply}]})

    def export_history(self) -> list[dict]:
        """Xuất history để lưu ra ngoài (Flask session, DB, JSON...)."""
        return list(self._history)

    def import_history(self, history: list[dict]) -> None:
        """Nạp lại history đã lưu."""
        self._history = list(history)

    def reset(self) -> None:
        """Xóa toàn bộ history, bắt đầu conversation mới."""
        self._history.clear()


# ---------------------------------------------------------------------------
# GeminiClient — core REST client
# ---------------------------------------------------------------------------

class GeminiClient:
    """
    Gọi Vertex AI Gemini qua REST API (không cần google-cloud-aiplatform SDK).

    Tính năng:
    - Xoay vòng region khi gặp quota 429/503 (round-robin)
    - Tự động fallback sang model dự phòng khi gặp 400/404
    - Hỗ trợ service account JSON key hoặc ADC (Application Default Credentials)
    - Thread-safe region rotation
    - Retry với exponential backoff

    Args:
        project_id:      GCP project ID
        model:           Model name (không cần prefix "google/"), VD: "gemini-3.5-flash-lite"
        fallback_model:  Model dự phòng khi model chính lỗi
        regions:         Danh sách region để xoay vòng khi bị quota
        key_file:        Đường dẫn tới service account JSON. Để trống = dùng ADC.
    """

    def __init__(
        self,
        project_id: str,
        model: str = "gemini-3.5-flash-lite",
        fallback_model: str = "gemini-3.5-flash",
        regions: list[str] | None = None,
        regions_us_eu: list[str] | None = None,
        regions_asia: list[str] | None = None,
        key_file: str = "",
        use_geo_optimization: bool = False,
    ) -> None:
        self.project = project_id
        self._model = model
        self._fallback = fallback_model
        self.regions = regions or ["us-central1"]
        self.regions_us_eu = regions_us_eu or ["us-central1", "us-east4", "europe-west1", "europe-west4"]
        self.regions_asia = regions_asia or ["asia-northeast1", "asia-southeast1", "asia-south1"]
        self.key_file = key_file
        self.use_geo_optimization = use_geo_optimization

        self._region_idx = 0
        self._creds = None
        self._lock = threading.Lock()

    # ── Credentials ─────────────────────────────────────────────────────────

    def _get_creds(self):
        """Trả về credentials hợp lệ; refresh nếu hết hạn."""
        if self._creds is None:
            if self.key_file and os.path.exists(self.key_file):
                from google.oauth2 import service_account
                self._creds = service_account.Credentials.from_service_account_file(
                    self.key_file,
                    scopes=["https://www.googleapis.com/auth/cloud-platform"],
                )
            else:
                self._creds, _ = google.auth.default(
                    scopes=["https://www.googleapis.com/auth/cloud-platform"]
                )
        if not self._creds.valid:
            self._creds.refresh(google.auth.transport.requests.Request())
        return self._creds

    # ── Region rotation & Geo-optimization ────────────────────────────────────

    def _get_regions_for_model(self, model: str) -> list[str]:
        """Get available regions for a specific model (with geo-optimization)."""
        if not self.use_geo_optimization:
            return self.regions

        # *-flash-lite (any generation): US/EU only (cheaper)
        if "flash-lite" in model:
            return self.regions_us_eu

        # non-lite *-flash / fallback model: everywhere (fallback for Asia)
        if "flash" in model or model == self._fallback:
            return self.regions_us_eu + self.regions_asia

        return self.regions

    def _current_region(self, model: str | None = None) -> str:
        active_model = model or self._model
        available_regions = self._get_regions_for_model(active_model)

        with self._lock:
            return available_regions[self._region_idx % len(available_regions)]

    def _rotate_region(self, model: str | None = None) -> str:
        active_model = model or self._model
        available_regions = self._get_regions_for_model(active_model)

        with self._lock:
            self._region_idx = (self._region_idx + 1) % len(available_regions)
            region = available_regions[self._region_idx]
        return region

    # ── Core REST call ───────────────────────────────────────────────────────

    def _call(
        self,
        contents: list[dict],
        max_retries: int = 3,
        temperature: float = 0.7,
        max_output_tokens: int = 8192,
        model: str | None = None,
    ) -> str:
        """
        Gọi generateContent REST endpoint.
        Xoay region khi 429/503; đổi sang fallback model khi 400/404.
        model: override tạm thời cho call này (không đổi self._model).
        """
        active_model = model or self._model
        payload = json.dumps({
            "contents": contents,
            "generation_config": {
                "temperature": temperature,
                "max_output_tokens": max_output_tokens,
            },
        }).encode("utf-8")

        last_error = "Unknown error"
        total_attempts = max_retries * len(self.regions)

        for attempt in range(total_attempts):
            region = self._current_region(active_model)
            url = build_vertex_endpoint(
                location=region, project=self.project, model=active_model, stream=False
            )

            try:
                creds = self._get_creds()
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={
                        "Authorization": f"Bearer {creds.token}",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=90) as resp:
                    body = json.loads(resp.read().decode("utf-8"))

                try:
                    candidate = body["candidates"][0]
                    text = candidate["content"]["parts"][0]["text"]
                    finish_reason = candidate.get("finishReason", "STOP")
                    if finish_reason == "MAX_TOKENS":
                        # Dùng separator \x00 để caller có thể tách partial text
                        raise RuntimeError(f"MAX_TOKENS_TRUNCATED\x00{text}")
                    return text
                except (KeyError, IndexError) as exc:
                    raise RuntimeError(f"Unexpected response shape: {body}") from exc

            except urllib.error.HTTPError as exc:
                status = exc.code
                err_body = exc.read().decode("utf-8", errors="replace")
                last_error = f"HTTP {status}: {err_body[:300]}"

                if status in (429, 503):
                    self._rotate_region(active_model)
                    time.sleep(2 ** min(attempt, 4))  # backoff tối đa 16s
                    continue

                if status in (400, 404):
                    if self._fallback and active_model != self._fallback:
                        active_model = self._fallback
                        self._rotate_region(self._fallback)
                        time.sleep(1)
                        continue
                    self._rotate_region(active_model)
                    time.sleep(1)
                    continue

                raise RuntimeError(last_error)

            except urllib.error.URLError as exc:
                last_error = str(exc)
                self._rotate_region(active_model)
                time.sleep(2)
                continue

        raise RuntimeError(
            f"Vertex AI failed after {total_attempts} attempts. Last error: {last_error}"
        )

    # ── Public API ───────────────────────────────────────────────────────────

    def ask(self, prompt: str, model: str | None = None) -> str:
        """
        One-shot call. Trả về toàn bộ response text.
        model: override model cho call này (VD: "gemini-2.0-flash-lite" cho task đơn giản).
        """
        return self._call(
            [{"role": "user", "parts": [{"text": prompt}]}],
            model=model,
        )

    def ask_with_image(
        self,
        image_bytes: bytes,
        mime_type: str,
        prompt: str,
        temperature: float = 0.2,
        max_output_tokens: int = 8192,
        model: str | None = None,
    ) -> str:
        """Multimodal one-shot: gửi ảnh + text prompt, trả về response text."""
        import base64
        b64_image = base64.b64encode(image_bytes).decode("utf-8")
        contents = [
            {
                "role": "user",
                "parts": [
                    {"inline_data": {"mime_type": mime_type, "data": b64_image}},
                    {"text": prompt},
                ],
            }
        ]
        return self._call(
            contents,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            model=model,
        )

    def _stream_call(self, contents: list[dict], model: str | None = None) -> Generator[str, None, None]:
        """
        Yield text chunks từ Vertex AI streamGenerateContent với contents tùy ý.
        Dùng chung cho cả one-shot và multi-turn chat streaming.
        Falls back to _call() nếu stream thất bại.
        """
        active_model = model or self._model
        region = self._current_region()
        url = build_vertex_endpoint(
            location=region, project=self.project, model=active_model, stream=True
        )
        payload = json.dumps({
            "contents": contents,
            "generation_config": {"temperature": 0.7, "max_output_tokens": 8192},
        }).encode("utf-8")

        try:
            creds = self._get_creds()
            req = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Authorization": f"Bearer {creds.token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                buf = b""
                for raw_chunk in iter(lambda: resp.read(512), b""):
                    buf += raw_chunk
                    while True:
                        start = buf.find(b"{")
                        if start < 0:
                            buf = b""
                            break
                        depth = 0
                        end = -1
                        in_str = False
                        esc = False
                        for i in range(start, len(buf)):
                            c = buf[i:i + 1]
                            if esc:
                                esc = False
                                continue
                            if c == b"\\" and in_str:
                                esc = True
                                continue
                            if c == b'"':
                                in_str = not in_str
                                continue
                            if not in_str:
                                if c == b"{":
                                    depth += 1
                                elif c == b"}":
                                    depth -= 1
                                    if depth == 0:
                                        end = i + 1
                                        break
                        if end < 0:
                            break
                        obj_bytes = buf[start:end]
                        buf = buf[end:]
                        try:
                            obj = json.loads(obj_bytes)
                            text = obj["candidates"][0]["content"]["parts"][0]["text"]
                            if text:
                                yield text
                        except (json.JSONDecodeError, KeyError, IndexError):
                            pass
        except Exception:
            yield self._call(contents, model=active_model)

    def stream(self, prompt: str) -> Generator[str, None, None]:
        """Legacy alias — delegates to stream_ask."""
        yield from self.stream_ask(prompt)

    def stream_ask(self, prompt: str, model: str | None = None) -> Generator[str, None, None]:
        """Yield text chunks cho single-turn prompt."""
        yield from self._stream_call(
            [{"role": "user", "parts": [{"text": prompt}]}],
            model=model,
        )

    def start_chat(self, history: list[dict] | None = None) -> ChatSession:
        """
        Tạo ChatSession multi-turn mới.

        Args:
            history: Danh sách history đã export từ session cũ (để khôi phục)
        """
        session = ChatSession(self)
        if history:
            session.import_history(history)
        return session

    def chat_message(self, session: ChatSession, message: str) -> str:
        """Gửi message vào ChatSession và trả về reply."""
        return session.send_message(message)

    # ── Factory method ───────────────────────────────────────────────────────

    @classmethod
    def from_ini(cls, ini_path: str) -> "GeminiClient":
        """
        Khởi tạo từ file config.ini (như ref/config.ini).

        Ví dụ config.ini:
            [vertexai]
            project_id = my-project
            model = gemini-3.5-flash-lite
            fallback_model = gemini-3.5-flash
            regions = us-central1, europe-west4

            [vision]
            key_file = ai_module/credentials/vertex/service-account.json
        """
        import configparser
        cfg = configparser.ConfigParser()
        cfg.read(ini_path, encoding="utf-8")

        project_id = cfg.get("vertexai", "project_id", fallback="")
        model = cfg.get("vertexai", "model", fallback="gemini-3.5-flash-lite")
        fallback = cfg.get("vertexai", "fallback_model", fallback="gemini-3.5-flash")

        # Support both old "regions" and new geo-optimized "regions_us_eu"/"regions_asia"
        raw_regions = cfg.get("vertexai", "regions", fallback="us-central1")
        regions = [r.strip() for r in raw_regions.split(",") if r.strip()]

        raw_regions_us_eu = cfg.get("vertexai", "regions_us_eu", fallback="")
        regions_us_eu = [r.strip() for r in raw_regions_us_eu.split(",") if r.strip()] if raw_regions_us_eu else None

        raw_regions_asia = cfg.get("vertexai", "regions_asia", fallback="")
        regions_asia = [r.strip() for r in raw_regions_asia.split(",") if r.strip()] if raw_regions_asia else None

        use_geo_optimization = cfg.getboolean("vertexai", "use_geo_optimization", fallback=False)
        key_file = cfg.get("vision", "key_file", fallback="")

        return cls(
            project_id=project_id,
            model=model,
            fallback_model=fallback,
            regions=regions,
            regions_us_eu=regions_us_eu,
            regions_asia=regions_asia,
            key_file=key_file,
            use_geo_optimization=use_geo_optimization,
        )
