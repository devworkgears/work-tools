"""設定の画面（/settings/）の API。どの画面からもリンクしない。"""

from typing import TYPE_CHECKING, Any, Callable

from . import settings

if TYPE_CHECKING:
    from .api import Api, Response


def settings_routes(api: "Api") -> list[tuple[str, str, Callable[..., "Response"]]]:
    from .api import _json_response

    def get(params: dict[str, str]) -> "Response":
        path = settings.path_for(api.store.path)
        return _json_response({"settings": settings.current().to_json(), "file": None if path is None else str(path),
                               "exists": path is not None and path.is_file(),
                               "defaults": {"product_kind": settings.PRODUCT_KIND, "part_kind": settings.PART_KIND}})

    def put(params: dict[str, str], body: dict[str, Any]) -> "Response":
        """settings の中身で書き換える。check: true は形を確かめて整えた中身を返すだけで、何も保存しない。"""
        new = settings.parse(body.get("settings"))
        if body.get("check"):
            return _json_response({"settings": new.to_json()})
        settings.save(api.store.path, new)
        return get(params)

    return [("GET", r"/api/settings", get), ("PUT", r"/api/settings", put)]
