"""Small, standard-library-only interface shared by agent adapters."""


class AdapterError(RuntimeError):
    """An adapter operation failed without a usable result."""


class AdapterUnavailable(AdapterError):
    """The agent or the requested, verified capability is unavailable."""


class BaseAdapter:
    name = "unknown"

    def detect(self):
        raise NotImplementedError

    def new_thread(self, root, name, sandbox, open_app=True):
        raise AdapterUnavailable(f"{self.name} worker creation is not verified or enabled")

    def deliver(self, thread, envelope):
        raise AdapterUnavailable(f"{self.name} delivery is not verified or enabled")

    def parse_hook(self, event, stdin_json):
        raise AdapterUnavailable(f"{self.name} hooks are not verified or enabled")

    def fallback_collect(self, request, timeout=0):
        raise AdapterUnavailable(f"{self.name} result collection is not verified or enabled")

    def hook_install_spec(self):
        raise AdapterUnavailable(f"{self.name} hook installation is not enabled")

    def classify_error(self, text):
        lowered = str(text).lower()
        if any(word in lowered for word in ("quota", "usage limit", "rate limit", "rate_limit", "insufficient_quota")):
            return "quota"
        if any(word in lowered for word in ("unauthorized", "authentication", "not logged in", "login required")):
            return "auth"
        if any(word in lowered for word in ("not installed", "not found", "no working")):
            return "not_installed"
        return "other"


Adapter = BaseAdapter
