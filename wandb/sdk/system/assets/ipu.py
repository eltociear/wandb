import multiprocessing as mp
from collections import deque
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple, Union

import wandb
import wandb.util
from wandb.sdk.system.assets.asset_registry import asset_registry
from wandb.sdk.system.assets.interfaces import (
    Interface,
    Metric,
    MetricsMonitor,
    MetricType,
)

if TYPE_CHECKING:
    from typing import Deque

    from wandb.sdk.internal.settings_static import SettingsStatic

gcipuinfo = wandb.util.get_module("gcipuinfo")


class IPUStats:
    """
    Stats for Graphcore IPU devices
    """

    name = "ipu.{}.{}"
    metric_type: MetricType = "gauge"
    samples: "Deque[dict]"

    # The metrics that change over time.
    # Only these are returned on each invocation
    # to avoid sending a load of unnecessary data.
    variable_metric_keys = {
        "average board temp",
        "average die temp",
        "clock",
        "ipu power",
        "ipu utilisation",
        "ipu utilisation (session)",
    }

    def __init__(self, pid: int, gc_ipu_info: Any = None) -> None:
        self.samples: "Deque[dict]" = deque()

        if gc_ipu_info is None:
            if not gcipuinfo:
                raise ImportError(
                    "Monitoring IPU stats requires gcipuinfo to be installed"
                )

            self._gc_ipu_info = gcipuinfo.gcipuinfo()
        else:
            self._gc_ipu_info = gc_ipu_info
        self._gc_ipu_info.setUpdateMode(True)

        self._pid = pid
        self._devices_called: Set[str] = set()

    @staticmethod
    def parse_metric(key: str, value: str) -> Optional[Tuple[str, Union[int, float]]]:
        metric_suffixes = {
            "temp": "C",
            "clock": "MHz",
            "power": "W",
            "utilisation": "%",
            "utilisation (session)": "%",
            "speed": "GT/s",
        }

        for metric, suffix in metric_suffixes.items():
            if key.endswith(metric) and value.endswith(suffix):
                value = value[: -len(suffix)]
                key = f"{key} ({suffix})"

        try:
            float_value = float(value)
            num_value = int(float_value) if float_value.is_integer() else float_value
        except ValueError:
            return None

        return key, num_value

    def sample(self) -> None:
        try:
            stats = {}
            devices = self._gc_ipu_info.getDevices()

            for device in devices:
                device_metrics: Dict[str, str] = dict(device)

                pid = device_metrics.get("user process id")
                if pid is None or int(pid) != self._pid:
                    continue

                device_id = device_metrics.get("id")
                initial_call = device_id not in self._devices_called
                if device_id is not None:
                    self._devices_called.add(device_id)

                for key, value in device_metrics.items():
                    log_metric = initial_call or key in self.variable_metric_keys
                    if not log_metric:
                        continue
                    parsed = self.parse_metric(key, value)
                    if parsed is None:
                        continue
                    parsed_key, parsed_value = parsed
                    stats[self.name.format(device_id, parsed_key)] = parsed_value

            self.samples.append(stats)

        except Exception as e:
            wandb.termwarn(f"IPU stats error {e}", repeat=False)

    def clear(self) -> None:
        self.samples.clear()

    def serialize(self) -> dict:
        if not self.samples:
            return {}
        stats = {}
        for key in self.samples[0].keys():
            samples = [s[key] for s in self.samples if key in s]
            aggregate = round(sum(samples) / len(samples), 2)
            stats[key] = aggregate
        return stats


@asset_registry.register
class IPU:
    def __init__(
        self,
        interface: "Interface",
        settings: "SettingsStatic",
        shutdown_event: mp.synchronize.Event,
    ) -> None:
        self.name = self.__class__.__name__.lower()
        self.metrics: List[Metric] = [
            IPUStats(settings._stats_pid),
        ]
        self.metrics_monitor = MetricsMonitor(
            self.metrics,
            interface,
            settings,
            shutdown_event,
        )

    @classmethod
    def is_available(cls) -> bool:
        try:
            import gcipuinfo  # type: ignore  # noqa: F401
        except ImportError:
            return False

        return True

    def start(self) -> None:
        self.metrics_monitor.start()

    def finish(self) -> None:
        self.metrics_monitor.finish()

    def probe(self) -> dict:
        num_devices = len(self.metrics[0]._gc_ipu_info.getDevices())  # type: ignore
        return {"ipu": {"device_count": num_devices, "vendor": "Graphcore"}}
