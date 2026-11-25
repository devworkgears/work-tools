"""設定の画面（/settings/）の API。どの画面からもリンクしない。"""

from typing import TYPE_CHECKING, Any, Callable

from . import settings
from .words import word_groups

if TYPE_CHECKING:
    from .api import Api, Response


def settings_routes(api: "Api") -> list[tuple[str, str, Callable[..., "Response"]]]:
    from .api import _json_response
    from .studyapi import StudyApi

    study = StudyApi(api)

    def get(params: dict[str, str]) -> "Response":
        path = settings.path_for(api.store.path)
        return _json_response({"settings": settings.current().to_json(), "file": None if path is None else str(path),
                               "exists": path is not None and path.is_file(),
                               "words": [{"name": g.name, "words": list(g.words), "imports": g.imports} for g in word_groups()],
                               "defaults": {"product_kind": settings.PRODUCT_KIND, "part_kind": settings.PART_KIND}})

    def put(params: dict[str, str], body: dict[str, Any]) -> "Response":
        """settings の中身で書き換える。check: true は形を確かめて整えた中身を返すだけで、何も保存しない。

        設定が変わったら、取り込んだ部品表を消す（bom_dropped）。
        """
        new = settings.parse(body.get("settings"))
        if body.get("check"):
            return _json_response({"settings": new.to_json()})
        study.store  # 保存する前の設定を部品表の設定として覚えさせる
        settings.save(api.store.path, new)
        return _json_response({**get(params).json(), "bom_dropped": study.drop_old_bom()})

    return [("GET", r"/api/settings", get), ("PUT", r"/api/settings", put)]
