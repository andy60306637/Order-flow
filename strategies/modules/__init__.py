"""策略模組套件：可組合的交易系統積木。"""
from strategies.modules.base_module import BaseModule, ModuleConfig
from strategies.modules.capital_management import CapitalConfig, CapitalModule
from strategies.modules.session_filter import SessionConfig, SessionModule
from strategies.modules.risk_management import RiskConfig, RiskModule
from strategies.modules.exit_management import ExitConfig, ExitModule
from strategies.modules.signal_trigger import SignalModule, StrategySignalModule

__all__ = [
    "BaseModule", "ModuleConfig",
    "CapitalConfig", "CapitalModule",
    "SessionConfig", "SessionModule",
    "RiskConfig", "RiskModule",
    "ExitConfig", "ExitModule",
    "SignalModule", "StrategySignalModule",
]
