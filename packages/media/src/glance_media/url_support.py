from functools import lru_cache

from yt_dlp.extractor import gen_extractor_classes


@lru_cache(maxsize=2048)
def has_dedicated_video_extractor(url: str) -> bool:
    """Return whether yt-dlp has a site-specific extractor without doing network I/O."""
    if not url:
        return False
    # 本机文件由确定性执行器直接读取，不需要 yt-dlp 站点提取器。
    if "://" not in url:
        return True
    for extractor in gen_extractor_classes():
        if extractor.IE_NAME == "generic":
            continue
        try:
            if extractor.suitable(url):
                return True
        except (AttributeError, TypeError, ValueError):
            continue
    return False
