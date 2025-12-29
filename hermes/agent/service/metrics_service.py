from typing import List

from apollo.egress.agent.service.metrics_service import BaseMetricsService


class MetricsService(BaseMetricsService):
    def fetch_metrics(self) -> List[str]:
        return []
