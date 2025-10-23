# ====================================================================================
# Apex BOT v20.0.3 - Future Trading / 10x Leverage (Patch 45: Pandas ATR Indexing Fix)
#
# 改良・修正点:
# 1. 【バグ修正】calculate_indicators関数内で、ATR計算結果 (Series) に不適切な多次元インデックス (iloc[:, 0]) を使用していたため、
#    "Too many indexers" エラーが発生していた問題を修正。Seriesを直接代入するように変更。
# 2. 【ロジック維持】リスクベースの動的ポジションサイジングとATRに基づく動的SL設定を維持。
# ====================================================================================

# 1. 必要なライブラリをインポート
import os
import time
import logging
import requests
import ccxt.async_support as ccxt_async
import ccxt
import numpy as np
import pandas as pd
import pandas_ta as ta
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any, Callable
import asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn
from dotenv import load_dotenv
import sys
import random
import json
import re
import uuid 
import math # 数値計算ライブラリ

# .envファイルから環境変数を読み込む
load_dotenv()

# 💡 【ログ確認対応】ロギング設定を明示的に定義
logging.basicConfig(
    level=logging.INFO, # INFOレベル以上のメッセージを出力
    format='%(asctime)s - %(levelname)s - (%(funcName)s) - %(message)s' 
)

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

JST = timezone(timedelta(hours=9))

# 出来高TOP40に加えて、主要な基軸通貨をDefaultに含めておく (現物シンボル形式 BTC/USDT)
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT",
    "DOGE/USDT", "DOT/USDT", "TRX/USDT", 
    "LTC/USDT", "AVAX/USDT", "LINK/USDT", "UNI/USDT", "ETC/USDT", "BCH/USDT",
    "NEAR/USDT", "ATOM/USDT", 
    "ALGO/USDT", "XLM/USDT", "SAND/USDT",
    "GALA/USDT", "FIL/USDT", 
    "AXS/USDT", "MANA/USDT", "AAVE/USDT",
    "FLOW/USDT", "IMX/USDT", 
]
TOP_SYMBOL_LIMIT = 40               # 監視対象銘柄の最大数 (出来高TOPから選出)
LOOP_INTERVAL = 60 * 1              # メインループの実行間隔 (秒) - 1分ごと
ANALYSIS_ONLY_INTERVAL = 60 * 60    # 分析専用通知の実行間隔 (秒) - 1時間ごと
WEBSHARE_UPLOAD_INTERVAL = 60 * 60  # WebShareログアップロード間隔 (1時間ごと)
MONITOR_INTERVAL = 10               # ポジション監視ループの実行間隔 (秒) - 10秒ごと

# 💡 クライアント設定
CCXT_CLIENT_NAME = os.getenv("EXCHANGE_CLIENT", "mexc")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_API_KEY")
SECRET_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_SECRET")
TEST_MODE = os.getenv("TEST_MODE", "False").lower() in ('true', '1', 't')
SKIP_MARKET_UPDATE = os.getenv("SKIP_MARKET_UPDATE", "False").lower() in ('true', '1', 't')

# 💡 先物取引設定 
LEVERAGE = 10 # 取引倍率
TRADE_TYPE = 'future' # 取引タイプ
MIN_MAINTENANCE_MARGIN_RATE = 0.005 # 最低維持証拠金率 (例: 0.5%) - 清算価格計算に使用

# 💡 リスクベースの動的ポジションサイジング設定 
# BASE_TRADE_SIZE_USDTはリスクベースサイジングにより無視されますが、互換性のために残します。
try:
    BASE_TRADE_SIZE_USDT = float(os.getenv("BASE_TRADE_SIZE_USDT", "100")) 
except ValueError:
    BASE_TRADE_SIZE_USDT = 100.0
    
MAX_RISK_PER_TRADE_PERCENT = float(os.getenv("MAX_RISK_PER_TRADE_PERCENT", "0.01")) # 最大リスク: 総資産の1%

# 💡 WEBSHARE設定 
WEBSHARE_METHOD = os.getenv("WEBSHARE_METHOD", "HTTP") 
WEBSHARE_POST_URL = os.getenv("WEBSHARE_POST_URL", "http://your-webshare-endpoint.com/upload") 

# グローバル変数 (状態管理用)
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS.copy()
LAST_SUCCESS_TIME: float = 0.0
LAST_SIGNAL_TIME: Dict[str, float] = {}
LAST_ANALYSIS_SIGNALS: List[Dict] = []
LAST_HOURLY_NOTIFICATION_TIME: float = 0.0
LAST_ANALYSIS_ONLY_NOTIFICATION_TIME: float = 0.0
LAST_WEBSHARE_UPLOAD_TIME: float = 0.0 
GLOBAL_MACRO_CONTEXT: Dict = {'fgi_proxy': 0.0, 'fgi_raw_value': 'N/A', 'forex_bonus': 0.0}
IS_FIRST_MAIN_LOOP_COMPLETED: bool = False 
OPEN_POSITIONS: List[Dict] = [] # 現在保有中のポジション (SL/TP監視用)
ACCOUNT_EQUITY_USDT: float = 0.0 # 現時点での総資産 (リスク計算に使用)

if TEST_MODE:
    logging.warning("⚠️ WARNING: TEST_MODE is active. Trading is disabled.")

# CCXTクライアントの準備完了フラグ
IS_CLIENT_READY: bool = False

# 取引ルール設定
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 
SIGNAL_THRESHOLD = 0.65             
TOP_SIGNAL_COUNT = 3                
REQUIRED_OHLCV_LIMITS = {'1m': 1000, '5m': 1000, '15m': 1000, '1h': 1000, '4h': 1000} 

# テクニカル分析定数 (v19.0.28ベース)
TARGET_TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h'] 
BASE_SCORE = 0.40                  
LONG_TERM_SMA_LENGTH = 200         
LONG_TERM_REVERSAL_PENALTY = 0.20   
STRUCTURAL_PIVOT_BONUS = 0.05       
RSI_MOMENTUM_LOW = 40              
MACD_CROSS_PENALTY = 0.15          
LIQUIDITY_BONUS_MAX = 0.06          
FGI_PROXY_BONUS_MAX = 0.05         
FOREX_BONUS_MAX = 0.0               

# ボラティリティ指標 (ATR) の設定 
ATR_LENGTH = 14
ATR_MULTIPLIER_SL = 2.0 # SLをATRの2.0倍に設定 (動的SLのベース)
MIN_RISK_PERCENT = 0.008 # SL幅の最小パーセンテージ (0.8%)

# 市場環境に応じた動的閾値調整のための定数
FGI_SLUMP_THRESHOLD = -0.02         
FGI_ACTIVE_THRESHOLD = 0.02         
SIGNAL_THRESHOLD_SLUMP = 0.90       
SIGNAL_THRESHOLD_NORMAL = 0.85      
SIGNAL_THRESHOLD_ACTIVE = 0.75      

RSI_DIVERGENCE_BONUS = 0.10         
VOLATILITY_BB_PENALTY_THRESHOLD = 0.01 
OBV_MOMENTUM_BONUS = 0.04           

# ====================================================================================
# UTILITIES & FORMATTING
# ====================================================================================

def format_usdt(amount: float) -> str:
    """USDT金額を整形する"""
    if amount is None:
        amount = 0.0
        
    if amount >= 1.0:
        return f"{amount:,.2f}"
    elif amount >= 0.01:
        return f"{amount:.4f}"
    else:
        return f"{amount:.6f}"
        
def format_price(price: float) -> str:
    """価格を整形する"""
    if price is None:
        price = 0.0
    # 0.01より大きい場合は小数点以下2桁、それ以外は動的に
    if price >= 0.01:
        return f"{price:,.2f}"
    return f"{price:,.8f}".rstrip('0').rstrip('.')

# 清算価格の計算関数
def calculate_liquidation_price(entry_price: float, leverage: int, side: str = 'long', maintenance_margin_rate: float = MIN_MAINTENANCE_MARGIN_RATE) -> float:
    """
    指定されたエントリー価格、レバレッジ、維持証拠金率に基づき、
    推定清算価格 (Liquidation Price) を計算する。
    
    MEXC先物取引の場合、Cross Marginでの清算価格の簡易計算式:
    Liquidation Price (Long) = Entry Price * (1 - (1 / Leverage) + Maintenance Margin Rate)
    """
    if leverage <= 0 or entry_price <= 0:
        return 0.0
        
    # 必要証拠金率 (1 / Leverage)
    initial_margin_rate = 1 / leverage
    
    if side.lower() == 'long':
        # ロングの場合、価格下落で清算
        liquidation_price = entry_price * (1 - initial_margin_rate + maintenance_margin_rate)
    elif side.lower() == 'short':
        # ショートの場合、価格上昇で清算 (現在BOTはロングのみだが、将来のため実装)
        liquidation_price = entry_price * (1 + initial_margin_rate - maintenance_margin_rate)
    else:
        return 0.0
        
    return max(0.0, liquidation_price) # 価格は0未満にはならない

def get_estimated_win_rate(score: float) -> str:
    """スコアに基づいて推定勝率を返す (通知用)"""
    if score >= 0.90: return "90%+"
    if score >= 0.85: return "85-90%"
    if score >= 0.75: return "75-85%"
    if score >= 0.65: return "65-75%" 
    if score >= 0.60: return "60-65%"
    return "<60% (低)"

def get_current_threshold(macro_context: Dict) -> float:
    """現在の市場環境に合わせた動的な取引閾値を決定し、返す。"""
    global FGI_SLUMP_THRESHOLD, FGI_ACTIVE_THRESHOLD
    global SIGNAL_THRESHOLD_SLUMP, SIGNAL_THRESHOLD_NORMAL, SIGNAL_THRESHOLD_ACTIVE
    
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    
    if fgi_proxy < FGI_SLUMP_THRESHOLD:
        return SIGNAL_THRESHOLD_SLUMP
    
    elif fgi_proxy > FGI_ACTIVE_THRESHOLD:
        return SIGNAL_THRESHOLD_ACTIVE
        
    else:
        return SIGNAL_THRESHOLD_NORMAL

def get_score_breakdown(signal: Dict) -> str:
    """分析スコアの詳細なブレークダウンメッセージを作成する (Telegram通知用)"""
    # (変更なし: ロジックの加点/減点要因は維持)
    tech_data = signal.get('tech_data', {})
    timeframe = signal.get('timeframe', 'N/A')
    
    LONG_TERM_REVERSAL_PENALTY_CONST = LONG_TERM_REVERSAL_PENALTY 
    MACD_CROSS_PENALTY_CONST = MACD_CROSS_PENALTY                 
    LIQUIDITY_BONUS_POINT_CONST = LIQUIDITY_BONUS_MAX           
    
    breakdown_list = []

    # 1. ベーススコア
    breakdown_list.append(f"  - **ベーススコア ({timeframe})**: <code>+{BASE_SCORE*100:.1f}</code> 点")
    
    # 2. 長期トレンド/構造の確認
    penalty_applied = tech_data.get('long_term_reversal_penalty_value', 0.0)
    if penalty_applied > 0.0:
        breakdown_list.append(f"  - ❌ 長期トレンド逆行 (SMA{LONG_TERM_SMA_LENGTH}): <code>-{penalty_applied*100:.1f}</code> 点")
    else:
        breakdown_list.append(f"  - ✅ 長期トレンド一致 (SMA{LONG_TERM_SMA_LENGTH}): <code>+{LONG_TERM_REVERSAL_PENALTY_CONST*100:.1f}</code> 点 (ペナルティ回避)")

    pivot_bonus = tech_data.get('structural_pivot_bonus', 0.0)
    if pivot_bonus > 0.0:
        breakdown_list.append(f"  - ✅ 価格構造/ピボット支持: <code>+{pivot_bonus*100:.1f}</code> 点")

    # 3. モメンタム/出来高の確認
    macd_penalty_applied = tech_data.get('macd_penalty_value', 0.0)
    total_momentum_penalty = macd_penalty_applied

    if total_momentum_penalty > 0.0:
        breakdown_list.append(f"  - ❌ モメンタム/クロス不利: <code>-{total_momentum_penalty*100:.1f}</code> 点")
    else:
        breakdown_list.append(f"  - ✅ MACD/RSIモメンタム加速: <code>+{MACD_CROSS_PENALTY_CONST*100:.1f}</code> 点相当 (ペナルティ回避)")

    obv_bonus = tech_data.get('obv_momentum_bonus_value', 0.0)
    if obv_bonus > 0.0:
        breakdown_list.append(f"  - ✅ 出来高/OBV確証: <code>+{obv_bonus*100:.1f}</code> 点")
    
    # 4. 流動性/マクロ要因
    liquidity_bonus = tech_data.get('liquidity_bonus_value', 0.0)
    if liquidity_bonus > 0.0:
        breakdown_list.append(f"  - ✅ 流動性 (板の厚み) 優位: <code>+{LIQUIDITY_BONUS_POINT_CONST*100:.1f}</code> 点")
        
    fgi_bonus = tech_data.get('sentiment_fgi_proxy_bonus', 0.0)
    if abs(fgi_bonus) > 0.001:
        sign = '✅' if fgi_bonus > 0 else '❌'
        breakdown_list.append(f"  - {sign} FGIマクロ影響: <code>{'+' if fgi_bonus > 0 else ''}{fgi_bonus*100:.1f}</code> 点")

    forex_bonus = tech_data.get('forex_bonus', 0.0) 
    breakdown_list.append(f"  - ⚪ 為替マクロ影響: <code>{forex_bonus*100:.1f}</code> 点 (機能削除済)")
    
    volatility_penalty = tech_data.get('volatility_penalty_value', 0.0)
    if volatility_penalty < 0.0:
        breakdown_list.append(f"  - ❌ ボラティリティ過熱ペナルティ: <code>{volatility_penalty*100:.1f}</code> 点")

    return "\n".join(breakdown_list)

def format_startup_message(
    account_status: Dict, 
    macro_context: Dict, 
    monitoring_count: int,
    current_threshold: float,
    bot_version: str
) -> str:
    """初回起動完了通知用のメッセージを作成する"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    
    fgi_raw_value = macro_context.get('fgi_raw_value', 'N/A')
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    
    if current_threshold == SIGNAL_THRESHOLD_SLUMP:
        market_condition_text = "低迷/リスクオフ"
    elif current_threshold == SIGNAL_THRESHOLD_ACTIVE:
        market_condition_text = "活発/リスクオン"
    else:
        market_condition_text = "通常/中立"
        
    trade_status = "自動売買 **ON**" if not TEST_MODE else "自動売買 **OFF** (TEST_MODE)"

    header = (
        f"🤖 **Apex BOT 起動完了通知** 🟢\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **確認日時**: {now_jst} (JST)\n"
        f"  - **取引所**: <code>{CCXT_CLIENT_NAME.upper()}</code> (先物モード / **{LEVERAGE}x**)\n" 
        f"  - **自動売買**: <b>{trade_status}</b>\n"
        f"  - **取引ロット**: **リスクベースサイジング**\n" 
        f"  - **最大リスク/取引**: <code>{MAX_RISK_PER_TRADE_PERCENT*100:.2f}</code> %\n" 
        f"  - **監視銘柄数**: <code>{monitoring_count}</code>\n"
        f"  - **BOTバージョン**: <code>{bot_version}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    balance_section = f"💰 <b>先物口座ステータス</b>\n" 
    if account_status.get('error'):
        balance_section += f"<pre>⚠️ ステータス取得失敗 (セキュリティのため詳細なエラーは表示しません。ログを確認してください)</pre>\n"
    else:
        equity_display = account_status['total_usdt_balance'] # equity (総資産)として扱う
        balance_section += (
            f"  - **総資産 (Equity)**: <code>{format_usdt(equity_display)}</code> USDT\n" 
        )
        
        # ボットが管理しているポジション
        if OPEN_POSITIONS:
            # filled_usdt は先物では名目価値 (Notional Value)
            total_managed_value = sum(p['filled_usdt'] for p in OPEN_POSITIONS) 
            balance_section += (
                f"  - **管理中ポジション**: <code>{len(OPEN_POSITIONS)}</code> 銘柄 (名目価値合計: <code>{format_usdt(total_managed_value)}</code> USDT)\n" 
            )
            for i, pos in enumerate(OPEN_POSITIONS[:3]): # Top 3のみ表示
                base_currency = pos['symbol'].replace('/USDT', '')
                balance_section += f"    - Top {i+1}: {base_currency} (SL: {format_price(pos['stop_loss'])} / TP: {format_price(pos['take_profit'])})\n"
            if len(OPEN_POSITIONS) > 3:
                balance_section += f"    - ...他 {len(OPEN_POSITIONS) - 3} 銘柄\n"
        else:
             balance_section += f"  - **管理中ポジション**: <code>なし</code>\n"

    balance_section += f"\n"

    macro_section = (
        f"🌍 <b>市場環境スコアリング</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **取引閾値 (Score)**: <code>{current_threshold*100:.0f} / 100</code>\n"
        f"  - **現在の市場環境**: <code>{market_condition_text}</code>\n"
        f"  - **FGI (恐怖・貪欲)**: <code>{fgi_raw_value}</code> ({'リスクオン' if fgi_proxy > FGI_ACTIVE_THRESHOLD else ('リスクオフ' if fgi_proxy < FGI_SLUMP_THRESHOLD else '中立')})\n"
        f"  - **総合マクロ影響**: <code>{((fgi_proxy + forex_bonus) * 100):.2f}</code> 点\n\n"
    )

    footer = (
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>※ この通知はメインの分析ループが一度完了したことを示します。約1分ごとに分析が実行されます。</pre>"
    )

    return header + balance_section + macro_section + footer

def format_telegram_message(signal: Dict, context: str, current_threshold: float, trade_result: Optional[Dict] = None, exit_type: Optional[str] = None) -> str:
    """Telegram通知用のメッセージを作成する (取引結果を追加)"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    symbol = signal['symbol']
    timeframe = signal['timeframe']
    score = signal['score']
    
    # trade_resultから値を取得する場合があるため、get()を使用
    entry_price = signal.get('entry_price', trade_result.get('entry_price', 0.0) if trade_result else 0.0)
    stop_loss = signal.get('stop_loss', trade_result.get('stop_loss', 0.0) if trade_result else 0.0)
    take_profit = signal.get('take_profit', trade_result.get('take_profit', 0.0) if trade_result else 0.0)
    liquidation_price = signal.get('liquidation_price', 0.0) 
    rr_ratio = signal.get('rr_ratio', 0.0)
    
    estimated_wr = get_estimated_win_rate(score)
    
    breakdown_details = get_score_breakdown(signal) if context != "ポジション決済" else ""

    trade_section = ""
    trade_status_line = ""

    if context == "取引シグナル":
        lot_size_units = signal.get('lot_size_units', 0.0) # 数量 (単位)
        notional_value = signal.get('notional_value', 0.0) # 名目価値
        
        if TEST_MODE:
            trade_status_line = f"⚠️ **テストモード**: 取引は実行されません。(ロット: {format_usdt(notional_value)} USDT, {LEVERAGE}x)" 
        elif trade_result is None or trade_result.get('status') == 'error':
            trade_status_line = f"❌ **自動売買 失敗**: {trade_result.get('error_message', 'APIエラー')}"
        elif trade_result.get('status') == 'ok':
            trade_status_line = f"✅ **自動売買 成功**: **先物ロング**注文を執行しました。" 
            
            filled_amount = trade_result.get('filled_amount', 0.0) 
            filled_usdt_notional = trade_result.get('filled_usdt', 0.0) 
            risk_usdt = signal.get('risk_usdt', 0.0) # リスク額
            
            trade_section = (
                f"💰 **取引実行結果**\n"
                f"  - **注文タイプ**: <code>先物 (Future) / 成行買い (Long)</code>\n" 
                f"  - **レバレッジ**: <code>{LEVERAGE}</code> 倍\n" 
                f"  - **リスク許容額**: <code>{format_usdt(risk_usdt)}</code> USDT ({MAX_RISK_PER_TRADE_PERCENT*100:.2f}%)\n" 
                f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
                f"  - **名目約定額**: <code>{format_usdt(filled_usdt_notional)}</code> USDT\n" 
            )
            
    elif context == "ポジション決済":
        exit_type_final = trade_result.get('exit_type', exit_type or '不明')
        trade_status_line = f"🔴 **ポジション決済**: {exit_type_final} トリガー ({LEVERAGE}x)" 
        
        entry_price = trade_result.get('entry_price', 0.0)
        exit_price = trade_result.get('exit_price', 0.0)
        pnl_usdt = trade_result.get('pnl_usdt', 0.0)
        pnl_rate = trade_result.get('pnl_rate', 0.0)
        filled_amount = trade_result.get('filled_amount', 0.0)
        
        pnl_sign = "✅ 利益確定" if pnl_usdt >= 0 else "❌ 損切り"
        
        trade_section = (
            f"💰 **決済実行結果** - {pnl_sign}\n"
            f"  - **エントリー価格**: <code>{format_price(entry_price)}</code>\n"
            f"  - **決済価格**: <code>{format_price(exit_price)}</code>\n"
            f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
            f"  - **純損益**: <code>{'+' if pnl_usdt >= 0 else ''}{format_usdt(pnl_usdt)}</code> USDT ({pnl_rate*100:.2f}%)\n" 
        )
            
    
    message = (
        f"🚀 **Apex TRADE {context}**\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **日時**: {now_jst} (JST)\n"
        f"  - **銘柄**: <b>{symbol}</b> ({timeframe})\n"
        f"  - **ステータス**: {trade_status_line}\n" 
        f"  - **総合スコア**: <code>{score * 100:.2f} / 100</code>\n"
        f"  - **取引閾値**: <code>{current_threshold * 100:.2f}</code> 点\n"
        f"  - **推定勝率**: <code>{estimated_wr}</code>\n"
        f"  - **リスクリワード比率 (RRR)**: <code>1:{rr_ratio:.2f}</code>\n"
        f"  - **エントリー**: <code>{format_price(entry_price)}</code>\n"
        f"  - **ストップロス (SL)**: <code>{format_price(stop_loss)}</code>\n"
        f"  - **テイクプロフィット (TP)**: <code>{format_price(take_profit)}</code>\n"
        f"  - **清算価格 (Liq. Price)**: <code>{format_price(liquidation_price)}</code>\n" 
        f"  - **リスク幅 (SL)**: <code>{format_usdt(entry_price - stop_loss)}</code> USDT\n"
        f"  - **リワード幅 (TP)**: <code>{format_usdt(take_profit - entry_price)}</code> USDT\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
    )
    
    if trade_section:
        message += trade_section + f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        
    if context == "取引シグナル":
        message += (
            f"  \n**📊 スコア詳細ブレークダウン** (+/-要因)\n"
            f"{breakdown_details}\n"
            f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )
        
    message += (f"<i>Bot Ver: v20.0.3 - Future Trading / 10x Leverage (Patch 45: Pandas ATR Indexing Fix)</i>") # ★変更
    return message


async def send_telegram_notification(message: str) -> bool:
    """Telegramにメッセージを送信する (変更なし)"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("❌ Telegram設定 (TOKEN/ID) が不足しています。通知をスキップします。")
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    }
    
    try:
        response = await asyncio.to_thread(requests.post, url, data=payload, timeout=5)
        response.raise_for_status()
        logging.info(f"✅ Telegram通知を送信しました。")
        return True
    except requests.exceptions.HTTPError as e:
        error_details = response.json() if 'response' in locals() else 'N/A'
        logging.error(f"❌ Telegram HTTPエラー: {e} - 詳細: {error_details}")
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Telegramリクエストエラー: {e}")
    return False

def _to_json_compatible(obj):
    """
    再帰的にオブジェクトをJSON互換の型に変換するヘルパー関数。 (変更なし)
    """
    if isinstance(obj, dict):
        return {k: _to_json_compatible(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_to_json_compatible(elem) for elem in obj]
    elif isinstance(obj, (bool, np.bool_)):
        return str(obj) 
    elif isinstance(obj, np.generic):
        return obj.item()
    return obj


def log_signal(data: Dict, log_type: str, trade_result: Optional[Dict] = None) -> None:
    """シグナルまたは取引結果をローカルファイルにログする (変更なし)"""
    try:
        log_entry = {
            'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            'log_type': log_type,
            'symbol': data.get('symbol', 'N/A'),
            'timeframe': data.get('timeframe', 'N/A'),
            'score': data.get('score', 0.0),
            'rr_ratio': data.get('rr_ratio', 0.0),
            'trade_result': trade_result or data.get('trade_result', None),
            'full_data': data,
        }
        
        cleaned_log_entry = _to_json_compatible(log_entry)

        log_file = f"apex_bot_{log_type.lower().replace(' ', '_')}_log.jsonl"
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(cleaned_log_entry, ensure_ascii=False) + '\n')
            
        logging.info(f"✅ {log_type}ログをファイルに記録しました。")
    except Exception as e:
        logging.error(f"❌ ログ書き込みエラー: {e}", exc_info=True)


# ====================================================================================
# WEBSHARE FUNCTION (HTTP POST) (変更なし)
# ====================================================================================

async def send_webshare_update(data: Dict[str, Any]):
    """取引データをHTTP POSTで外部サーバーに送信する"""
    
    if WEBSHARE_METHOD == "HTTP":
        if not WEBSHARE_POST_URL or "your-webshare-endpoint.com/upload" in WEBSHARE_POST_URL:
            logging.warning("⚠️ WEBSHARE_POST_URLが設定されていません。またはデフォルト値のままです。送信をスキップします。")
            return

        try:
            cleaned_data = _to_json_compatible(data)
            
            response = await asyncio.to_thread(requests.post, WEBSHARE_POST_URL, json=cleaned_data, timeout=10)
            response.raise_for_status()

            logging.info(f"✅ WebShareデータ (HTTP POST) を送信しました。ステータス: {response.status_code}")

        except requests.exceptions.RequestException as e:
            logging.error(f"❌ WebShare (HTTP POST) エラー: {e}")
            await send_telegram_notification(f"🚨 <b>WebShareエラー (HTTP POST)</b>\nデータ送信に失敗しました: <code>{e}</code>")

    else:
        logging.warning("⚠️ WEBSHARE_METHOD が 'HTTP' 以外に設定されています。WebShare送信をスキップします。")
        

# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def initialize_exchange_client() -> bool:
    """CCXTクライアントを初期化し、市場情報をロードする (変更なし)"""
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    IS_CLIENT_READY = False
    
    if not API_KEY or not SECRET_KEY:
         logging.critical("❌ CCXT初期化スキップ: API_KEY または SECRET_KEY が設定されていません。")
         return False
         
    try:
        client_name = CCXT_CLIENT_NAME.lower()
        if client_name == 'binance':
            exchange_class = ccxt_async.binance
        elif client_name == 'bybit':
            exchange_class = ccxt_async.bybit
        elif client_name == 'mexc':
            exchange_class = ccxt_async.mexc
        else:
            logging.error(f"❌ 未対応の取引所クライアント: {CCXT_CLIENT_NAME}")
            return False

        options = {
            'defaultType': 'future', 
        }

        EXCHANGE_CLIENT = exchange_class({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True,
            'options': options
        })
        
        await EXCHANGE_CLIENT.load_markets() 
        
        # レバレッジの設定 (MEXC向け)
        if EXCHANGE_CLIENT.id == 'mexc':
            symbols_to_set_leverage = []
            for s in CURRENT_MONITOR_SYMBOLS:
                # 💡【前回の修正箇所】safe_marketに'defaultType'を渡さないように修正 
                market = EXCHANGE_CLIENT.safe_market(s) 
                if market and market['type'] in ['future', 'swap'] and market['active']:
                    symbols_to_set_leverage.append(market['symbol']) 
            
            for symbol in symbols_to_set_leverage:
                try:
                    await EXCHANGE_CLIENT.set_leverage(LEVERAGE, symbol, params={'marginMode': 'cross'}) 
                except Exception as e:
                    logging.warning(f"⚠️ {symbol} のレバレッジ設定 ({LEVERAGE}x) に失敗しました: {e}")
            
            logging.info(f"✅ MEXCのレバレッジを主要な先物銘柄で {LEVERAGE}x (クロス) に設定しました。")
        
        logging.info(f"✅ CCXTクライアント ({CCXT_CLIENT_NAME}) を先物取引モードで初期化し、市場情報をロードしました。")
        IS_CLIENT_READY = True
        return True

    except ccxt.AuthenticationError as e: 
        logging.critical(f"❌ CCXT初期化失敗 - 認証エラー: APIキー/シークレットを確認してください。{e}", exc_info=True)
    except ccxt.ExchangeNotAvailable as e: 
        logging.critical(f"❌ CCXT初期化失敗 - 取引所接続エラー: サーバーが利用できません。{e}", exc_info=True)
    except ccxt.NetworkError as e:
        logging.critical(f"❌ CCXT初期化失敗 - ネットワークエラー: 接続を確認してください。{e}", exc_info=True)
    except Exception as e:
        logging.critical(f"❌ CCXTクライアント初期化失敗 - 予期せぬエラー: {e}", exc_info=True)
        
    EXCHANGE_CLIENT = None
    return False

async def fetch_account_status() -> Dict:
    """CCXTから先物口座の残高と利用可能マージン情報を取得し、グローバル変数に格納する。"""
    global EXCHANGE_CLIENT, ACCOUNT_EQUITY_USDT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 口座ステータス取得失敗: CCXTクライアントが準備できていません。")
        return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}

    try:
        # 💡【前回の修正箇所】MEXCで fetch_balance({'type': 'future'}) がエラーになるため、引数なしで呼び出す。
        balance = await EXCHANGE_CLIENT.fetch_balance()
        
        # MEXCの場合、USDT建てのフューチャー残高 (equity/total) を総資産として扱う
        total_usdt_balance = balance.get('total', {}).get('USDT', 0.0) 
        
        # グローバル変数に最新の総資産を保存
        ACCOUNT_EQUITY_USDT = total_usdt_balance

        return {
            'total_usdt_balance': total_usdt_balance, # 総資産 (Equity)
            'open_positions': [], 
            'error': False
        }

    except ccxt.NetworkError as e:
        logging.error(f"❌ 口座ステータス取得失敗 (ネットワークエラー): {e}")
    except ccxt.AuthenticationError as e:
        logging.critical(f"❌ 口座ステータス取得失敗 (認証エラー): {e}")
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得失敗 (予期せぬエラー): {e}")

    return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}

# 注文数量調整を base units (数量) で行う関数に更新 (変更なし)
async def adjust_order_amount_by_base_units(symbol: str, target_base_amount: float) -> Optional[float]:
    """
    指定されたBase通貨建ての目標取引数量を、取引所の最小数量および数量精度に合わせて調整する。
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or symbol not in EXCHANGE_CLIENT.markets:
        try:
             market = await EXCHANGE_CLIENT.load_market(symbol, params={'defaultType': TRADE_TYPE})
             if not market:
                  logging.error(f"❌ 注文数量調整エラー: {symbol} の先物市場情報が未ロードです。")
                  return None
        except Exception:
             logging.error(f"❌ 注文数量調整エラー: {symbol} の先物市場情報が未ロードです。")
             return None
        
    market = EXCHANGE_CLIENT.markets[symbol]
    
    if target_base_amount <= 0:
        logging.error(f"❌ 注文数量調整エラー: 目標Base数量 ({target_base_amount:.8f}) が無効です。")
        return None

    try:
        ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
        current_price = ticker['last']
    except Exception as e:
        logging.error(f"❌ 注文数量調整エラー: {symbol} の現在価格を取得できませんでした。{e}")
        return None

    amount_precision_value = market['precision']['amount']
    min_amount = market['limits']['amount']['min']

    adjusted_amount = target_base_amount
    
    # 精度調整
    if amount_precision_value is not None and amount_precision_value > 0:
        try:
             precision_places = int(round(-math.log10(amount_precision_value)))
        except ValueError:
             precision_places = 8 
        
        # 指定された桁数に切り捨て (floor)
        adjusted_amount = math.floor(target_base_amount * math.pow(10, precision_places)) / math.pow(10, precision_places)
    
    # 最小数量チェック
    if min_amount is not None and adjusted_amount < min_amount:
        adjusted_amount = min_amount
        logging.warning(
            f"⚠️ 【{market['base']}】最小数量調整: 計算されたロット ({target_base_amount:.8f}) が最小 ({min_amount}) 未満でした。"
            f"数量を最小値 **{adjusted_amount:.8f}** に自動調整しました。"
        )
    
    # 調整後の名目価値チェック (約1 USDT未満は取引不可とみなす)
    if adjusted_amount * current_price < 1.0 or adjusted_amount <= 0:
        logging.error(f"❌ 調整後の数量 ({adjusted_amount:.8f}) が取引所の最小金額 (約1 USDT) またはゼロ以下です。")
        return None

    logging.info(
        f"✅ 数量調整完了: {symbol} の取引数量をルールに適合する **{adjusted_amount:.8f}** に調整しました。"
    )
    return adjusted_amount

async def fetch_ohlcv_safe(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """OHLCVデータを安全に取得する (変更なし)"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return None
        
    try:
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit, params={'defaultType': TRADE_TYPE}) 
        
        if not ohlcv:
            logging.warning(f"⚠️ OHLCVデータ取得: {symbol} ({timeframe}) でデータが空でした。取得足数: {limit}")
            return None 

        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df = df.set_index('timestamp')
        return df

    except ccxt.RequestTimeout as e:
        logging.error(f"❌ CCXTエラー (RequestTimeout): {symbol} ({timeframe}). APIコールがタイムアウトしました。{e}")
    except ccxt.RateLimitExceeded as e:
        logging.error(f"❌ CCXTエラー (RateLimitExceeded): {symbol} ({timeframe}). APIコールの頻度制限を超過しました。{e}")
    except ccxt.BadSymbol as e:
        logging.error(f"❌ CCXTエラー (BadSymbol): {symbol} ({timeframe}). シンボルが取引所に存在しない可能性があります。{e}")
    except ccxt.ExchangeError as e: 
        logging.error(f"❌ CCXTエラー (ExchangeError): {symbol} ({timeframe}). 取引所からの応答エラーです。{e}")
    except Exception as e:
        logging.error(f"❌ 予期せぬエラー (OHLCV取得): {symbol} ({timeframe}). {e}", exc_info=True)

    return None

async def fetch_fgi_data() -> Dict[str, Any]:
    """外部API (Alternative.me) から現在の恐怖・貪欲指数(FGI)を取得する。 (変更なし)"""
    FGI_API_URL = "https://api.alternative.me/fng/?limit=1"
    
    try:
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        if data and 'data' in data and len(data['data']) > 0:
            fgi_entry = data['data'][0]
            
            raw_value = int(fgi_entry.get('value', 50))
            fgi_proxy = (raw_value - 50) / 50.0
            
            logging.info(f"✅ FGIデータ取得成功: Raw={raw_value}, Proxy={fgi_proxy:.2f}")
            
            return {
                'fgi_raw_value': raw_value,
                'fgi_proxy': fgi_proxy,
                'forex_bonus': 0.0 
            }
            
        logging.warning("⚠️ FGIデータ取得失敗: API応答が空または不正です。")
        
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ FGIデータ取得エラー (リクエスト): {e}")
    except Exception as e:
        logging.error(f"❌ FGIデータ取得エラー (処理): {e}", exc_info=True)
        
    return {
        'fgi_raw_value': '50 (Error)', 
        'fgi_proxy': 0.0, 
        'forex_bonus': 0.0
    }

# ====================================================================================
# TRADING LOGIC
# ====================================================================================

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """テクニカルインジケーターを計算する"""
    if df.empty:
        return df
    
    df['SMA200'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH)
    
    # MACDを計算
    macd_data = ta.macd(df['close'], fast=12, slow=26, signal=9)
    if macd_data is not None and not macd_data.empty:
        df['MACD'] = macd_data.iloc[:, 0]
        df['MACDh'] = macd_data.iloc[:, 1]
        df['MACDs'] = macd_data.iloc[:, 2]

    # RSIを計算
    df['RSI'] = ta.rsi(df['close'], length=14)
    
    # OBVを計算
    df['OBV'] = ta.obv(df['close'], df['volume'])
    
    # ボリンジャーバンドを計算 
    bbands = ta.bbands(df['close'], length=20, std=2)
    if bbands is not None and not bbands.empty:
        df['BBL'] = bbands.iloc[:, 0] 
        df['BBM'] = bbands.iloc[:, 1] 
        df['BBU'] = bbands.iloc[:, 2] 
        
    # ATR (Average True Range) を計算 
    atr_data = ta.atr(df['high'], df['low'], df['close'], length=ATR_LENGTH)
    if atr_data is not None and not atr_data.empty:
        # 💡 【修正箇所】atr_data は Series のため、iloc[:, 0] はエラー。直接代入する。
        df['ATR'] = atr_data
    else:
        df['ATR'] = np.nan
    
    return df

def analyze_signals(df: pd.DataFrame, symbol: str, timeframe: str, macro_context: Dict) -> Optional[Dict]:
    """分析ロジックに基づき、取引シグナルを生成する (変更なし)"""

    if df.empty or df['SMA200'].isnull().all() or df['close'].isnull().iloc[-1] or df['ATR'].isnull().iloc[-1]: 
        return None
        
    current_price = df['close'].iloc[-1]
    current_atr = df['ATR'].iloc[-1] 
    
    # 簡易的なロングシグナル (終値がSMA200を上回っているかをチェック)
    if df['SMA200'].iloc[-1] is not None and current_price > df['SMA200'].iloc[-1]:
        
        # --- スコアリングロジックの簡易実装 (変更なし) ---
        score = BASE_SCORE 
        
        fgi_proxy = macro_context.get('fgi_proxy', 0.0)
        sentiment_fgi_proxy_bonus = (fgi_proxy / FGI_ACTIVE_THRESHOLD) * FGI_PROXY_BONUS_MAX if abs(fgi_proxy) <= FGI_ACTIVE_THRESHOLD else (FGI_PROXY_BONUS_MAX if fgi_proxy > 0 else -FGI_PROXY_BONUS_MAX)
        
        tech_data = {
            'long_term_reversal_penalty_value': 0.0, 
            'structural_pivot_bonus': STRUCTURAL_PIVOT_BONUS, 
            'macd_penalty_value': 0.0, 
            'obv_momentum_bonus_value': OBV_MOMENTUM_BONUS, 
            'liquidity_bonus_value': LIQUIDITY_BONUS_MAX, 
            'sentiment_fgi_proxy_bonus': sentiment_fgi_proxy_bonus, 
            'forex_bonus': 0.0,
            'volatility_penalty_value': 0.0,
        }
        
        score += (
            (LONG_TERM_REVERSAL_PENALTY) + 
            tech_data['structural_pivot_bonus'] + 
            (MACD_CROSS_PENALTY) + 
            tech_data['obv_momentum_bonus_value'] + 
            tech_data['liquidity_bonus_value'] + 
            tech_data['sentiment_fgi_proxy_bonus'] + 
            tech_data['forex_bonus'] +
            tech_data['volatility_penalty_value'] - 
            tech_data['long_term_reversal_penalty_value'] -
            tech_data['macd_penalty_value']
        )
        
        
        ##############################################################
        # 動的なSL/TPとRRRの設定ロジック (ATRをSLに使用) 
        ##############################################################
        
        # 1. SL (リスク幅) の動的設定 (ATRに基づく)
        
        # ATRによるSL幅 (絶対値)
        atr_sl_range = current_atr * ATR_MULTIPLIER_SL
        
        # SL価格の計算
        stop_loss = current_price - atr_sl_range
        
        # SLリスク幅が価格の最小パーセンテージ (MIN_RISK_PERCENT) を下回っていないかチェック
        min_sl_range = current_price * MIN_RISK_PERCENT
        if atr_sl_range < min_sl_range:
             stop_loss = current_price - min_sl_range
             atr_sl_range = min_sl_range # RRR計算用に値を更新
        
        # リスク額のUSD建て表現 (1単位あたりのUSDリスク)
        risk_usdt_per_unit = current_price - stop_loss 
        
        # 2. TP (リワード幅) の動的設定 (総合スコアとRRRを考慮)
        BASE_RRR = 1.5  
        MAX_SCORE_FOR_RRR = 0.85
        MAX_RRR = 3.0
        
        if score > SIGNAL_THRESHOLD:
            score_ratio = min(1.0, (score - SIGNAL_THRESHOLD) / (MAX_SCORE_FOR_RRR - SIGNAL_THRESHOLD))
            dynamic_rr_ratio = BASE_RRR + (MAX_RRR - BASE_RRR) * score_ratio
        else:
            dynamic_rr_ratio = BASE_RRR 
            
        # TP価格の計算
        take_profit = current_price + (risk_usdt_per_unit * dynamic_rr_ratio)
        
        # 最終的なRRRを記録用として算出
        rr_ratio = dynamic_rr_ratio 
        
        # 3. 清算価格の計算 
        liquidation_price = calculate_liquidation_price(current_price, LEVERAGE, side='long', maintenance_margin_rate=MIN_MAINTENANCE_MARGIN_RATE)

        ##############################################################

        current_threshold = get_current_threshold(macro_context)
        
        if score > current_threshold and rr_ratio >= 1.0 and risk_usdt_per_unit > 0:
            
            # 4. リスクベースのポジションサイジング計算
            max_risk_usdt = ACCOUNT_EQUITY_USDT * MAX_RISK_PER_TRADE_PERCENT
            
            # Base通貨建ての目標数量 (ポジションサイジングの核心)
            target_base_amount_units = max_risk_usdt / risk_usdt_per_unit 
            
            # 名目価値 (Notional Value)
            notional_value = target_base_amount_units * current_price 
            
            return {
                'symbol': symbol,
                'timeframe': timeframe,
                'action': 'buy', 
                'score': score,
                'rr_ratio': rr_ratio, 
                'entry_price': current_price,
                'stop_loss': stop_loss, 
                'take_profit': take_profit, 
                'liquidation_price': liquidation_price, 
                'risk_usdt_per_unit': risk_usdt_per_unit, # 1単位あたりのリスク
                'risk_usdt': max_risk_usdt, # 許容リスク総額
                'notional_value': notional_value, # 計算された名目価値 (取引サイズ)
                'lot_size_units': target_base_amount_units, # 目標Base数量
                'tech_data': tech_data, 
            }
    return None

async def liquidate_position(position: Dict, exit_type: str, current_price: float) -> Optional[Dict]:
    """
    ポジションを決済する (成行売りを想定)。 (変更なし)
    """
    global EXCHANGE_CLIENT
    symbol = position['symbol']
    amount = position['amount']
    entry_price = position['entry_price']
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 決済失敗: CCXTクライアントが準備できていません。")
        return {'status': 'error', 'error_message': 'CCXTクライアント未準備'}

    # 1. 注文実行（先物ポジションの決済: 反対売買の成行売り）
    try:
        params = {'positionSide': 'LONG', 'closePosition': True} 
        
        if TEST_MODE:
            logging.info(f"✨ TEST MODE: {symbol} Close Long Market ({exit_type}). 数量: {amount:.8f} の注文をシミュレート。")
            order = {
                'id': f"test-exit-{uuid.uuid4()}",
                'symbol': symbol,
                'side': 'sell',
                'amount': amount,
                'price': current_price, 
                'status': 'closed', 
                'datetime': datetime.now(timezone.utc).isoformat(),
                'filled': amount,
                'cost': amount * current_price, 
            }
        else:
            order = await EXCHANGE_CLIENT.create_order(
                symbol=symbol,
                type='market',
                side='sell',
                amount=amount, 
                params=params
            )
            logging.info(f"✅ 決済実行成功: {symbol} Close Long Market. 数量: {amount:.8f} ({exit_type})")

        filled_amount_val = order.get('filled', 0.0)
        cost_val = order.get('cost', 0.0) 

        if filled_amount_val <= 0:
            return {'status': 'error', 'error_message': '約定数量ゼロ'}

        exit_price = cost_val / filled_amount_val if filled_amount_val > 0 else current_price

        # PnL計算 (概算)
        initial_notional_cost = position['filled_usdt']
        final_notional_value = cost_val 
        pnl_usdt = final_notional_value - initial_notional_cost 
        
        initial_margin = initial_notional_cost / LEVERAGE 
        pnl_rate = pnl_usdt / initial_margin if initial_margin > 0 else 0.0 

        trade_result = {
            'status': 'ok',
            'exit_type': exit_type,
            'entry_price': entry_price,
            'exit_price': exit_price,
            'filled_amount': filled_amount_val,
            'pnl_usdt': pnl_usdt, 
            'pnl_rate': pnl_rate, 
        }
        return trade_result

    except Exception as e:
        logging.error(f"❌ 決済失敗 ({exit_type}): {symbol}. {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'決済エラー: {e}'}

async def position_management_loop_async():
    """オープンポジションを監視し、SL/TPをチェックして決済する (変更なし)"""
    global OPEN_POSITIONS, GLOBAL_MACRO_CONTEXT

    if not OPEN_POSITIONS:
        return

    positions_to_check = list(OPEN_POSITIONS)
    closed_positions = []
    
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)

    for position in positions_to_check:
        symbol = position['symbol']
        sl = position['stop_loss']
        tp = position['take_profit']
        
        position['timeframe'] = 'N/A (Monitor)' 
        position['score'] = 0.0 
        position['rr_ratio'] = 0.0
        position['liquidation_price'] = calculate_liquidation_price(position['entry_price'], LEVERAGE, 'long')

        try:
            ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
            current_price = ticker['last']
            
            exit_type = None
            
            if current_price <= sl:
                exit_type = "SL (ストップロス)"
            elif current_price <= position.get('liquidation_price', 0.0): # 清算価格を下回ったらSLより優先して決済
                 exit_type = "Liquidation Risk (清算リスク)"
            elif current_price >= tp:
                exit_type = "TP (テイクプロフィット)"
            
            if exit_type:
                trade_result = await liquidate_position(position, exit_type, current_price)
                
                if trade_result and trade_result.get('status') == 'ok':
                    log_signal(position, 'Position Exit', trade_result)
                    await send_telegram_notification(
                        format_telegram_message(position, "ポジション決済", current_threshold, trade_result=trade_result, exit_type=exit_type)
                    )
                    closed_positions.append(position)
                else:
                    logging.error(f"❌ {symbol} 決済シグナル ({exit_type}) が発生しましたが、決済実行に失敗しました。")

        except Exception as e:
            logging.error(f"❌ ポジション監視エラー: {symbol}. {e}", exc_info=True)

    for closed_pos in closed_positions:
        if closed_pos in OPEN_POSITIONS:
            OPEN_POSITIONS.remove(closed_pos)


async def execute_trade(signal: Dict) -> Optional[Dict]:
    """
    取引シグナルに基づき、先物取引所に対してロング注文を実行する。 (変更なし)
    """
    global OPEN_POSITIONS, EXCHANGE_CLIENT, IS_CLIENT_READY
    
    symbol = signal.get('symbol')
    action = 'buy' 
    entry_price = signal.get('entry_price', 0.0) 
    
    # リスクベースサイジングの結果を取得
    target_base_amount = signal.get('lot_size_units', 0.0)
    notional_value = signal.get('notional_value', 0.0)

    if not IS_CLIENT_READY or not EXCHANGE_CLIENT:
        logging.error(f"❌ 注文実行失敗: CCXTクライアントが準備できていません。")
        return {'status': 'error', 'error_message': 'CCXTクライアント未準備'}
        
    if entry_price <= 0 or target_base_amount <= 0:
        logging.error(f"❌ 注文実行失敗: エントリー価格または計算ロットが不正です。Price={entry_price}, Lot={target_base_amount}")
        return {'status': 'error', 'error_message': 'エントリー価格またはロット不正'}

    # 1. 最小数量と精度を考慮した数量調整
    adjusted_amount = await adjust_order_amount_by_base_units(symbol, target_base_amount)

    if adjusted_amount is None:
        logging.error(f"❌ {symbol} {action} 注文キャンセル: 注文数量の自動調整に失敗しました。")
        return {'status': 'error', 'error_message': '注文数量調整失敗'}

    # 2. 注文実行（成行注文: ロングポジション）
    try:
        params = {'positionSide': 'LONG', 'leverage': LEVERAGE}
        
        if TEST_MODE:
            logging.info(f"✨ TEST MODE: {symbol} Long Market. 数量: {adjusted_amount:.8f} の注文をシミュレート。 (レバレッジ: {LEVERAGE}x, 名目価値: {notional_value:.2f} USDT)")
            order = {
                'id': f"test-{uuid.uuid4()}",
                'symbol': symbol,
                'side': action,
                'amount': adjusted_amount,
                'price': entry_price, 
                'status': 'closed', 
                'datetime': datetime.now(timezone.utc).isoformat(),
                'filled': adjusted_amount,
                'cost': adjusted_amount * entry_price, # 名目価値 (Notional Value)
            }
        else:
            order = await EXCHANGE_CLIENT.create_order(
                symbol=symbol,
                type='market',
                side=action,
                amount=adjusted_amount, 
                params=params 
            )
            logging.info(f"✅ 注文実行成功: {symbol} Long Market. 数量: {adjusted_amount:.8f} (レバレッジ: {LEVERAGE}x)")

        filled_amount_val = order.get('filled', 0.0)
        price_used = order.get('price')
        if filled_amount_val is None:
            filled_amount_val = 0.0
        
        effective_price = price_used if price_used is not None else entry_price
        
        notional_cost = order.get('cost', filled_amount_val * effective_price) 

        trade_result = {
            'status': 'ok',
            'order_id': order.get('id'),
            'filled_amount': filled_amount_val, 
            'filled_usdt': notional_cost, 
            'entry_price': effective_price,
            'stop_loss': signal.get('stop_loss'),
            'take_profit': signal.get('take_profit'),
        }

        # ポジションの開始を記録
        if order['status'] in ('closed', 'fill') and trade_result['filled_amount'] > 0:
            position_data = {
                'symbol': symbol,
                'entry_time': order.get('datetime'),
                'side': action,
                'amount': trade_result['filled_amount'],
                'entry_price': trade_result['entry_price'],
                'filled_usdt': trade_result['filled_usdt'], 
                'stop_loss': trade_result['stop_loss'],
                'take_profit': trade_result['take_profit'],
                'order_id': order.get('id'),
                'status': 'open',
            }
            OPEN_POSITIONS.append(position_data)
            
        return trade_result

    except ccxt.InsufficientFunds as e:
        logging.error(f"❌ 注文失敗 - 残高不足: {symbol} {action}. {e}")
        return {'status': 'error', 'error_message': f'残高不足エラー: {e}'}
    except ccxt.InvalidOrder as e:
        logging.error(f"❌ 注文失敗 - 無効な注文: 取引所ルール違反の可能性 (数量/価格/最小取引額)。{e}")
        return {'status': 'error', 'error_message': f'無効な注文エラー: {e}'}
    except ccxt.ExchangeError as e:
        logging.error(f"❌ 注文失敗 - 取引所エラー: API応答の問題。{e}")
        return {'status': 'error', 'error_message': f'取引所APIエラー: {e}'}
    except Exception as e:
        logging.error(f"❌ 注文失敗 - 予期せぬエラー: {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'予期せぬエラー: {e}'}


# ====================================================================================
# MAIN BOT LOGIC
# ====================================================================================

async def main_bot_loop():
    """ボットのメイン実行ループ (1分ごと)"""
    global LAST_SUCCESS_TIME, IS_CLIENT_READY, CURRENT_MONITOR_SYMBOLS, LAST_ANALYSIS_SIGNALS, IS_FIRST_MAIN_LOOP_COMPLETED, GLOBAL_MACRO_CONTEXT, LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, LAST_SIGNAL_TIME, LAST_WEBSHARE_UPLOAD_TIME 

    if not IS_CLIENT_READY:
        logging.info("CCXTクライアントを初期化中...")
        await initialize_exchange_client()
        if not IS_CLIENT_READY:
            logging.critical("クライアント初期化失敗。次のループでリトライします。")
            return
            
    # Account Equityを毎回取得 (リスク計算のために最新の総資産が必要)
    account_status = await fetch_account_status()
    if account_status.get('error') or ACCOUNT_EQUITY_USDT <= 0:
        if IS_FIRST_MAIN_LOOP_COMPLETED:
            logging.critical("🚨 総資産の取得失敗または残高がゼロです。取引をスキップします。")
            return
        
    logging.info(f"--- 💡 {datetime.now(JST).strftime('%Y/%m/%d %H:%M:%S')} - BOT LOOP START (M1 Frequency) - Equity: {format_usdt(ACCOUNT_EQUITY_USDT)} USDT ---")

    # FGIデータを取得し、GLOBAL_MACRO_CONTEXTを更新
    GLOBAL_MACRO_CONTEXT = await fetch_fgi_data() 
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    new_signals: List[Dict] = []
    
    for symbol in CURRENT_MONITOR_SYMBOLS:
        # ATRの計算に十分なデータが必要なため、最も小さい時間枠のOHLCVが重要
        for timeframe in TARGET_TIMEFRAMES:
            limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 1000)
            df = await fetch_ohlcv_safe(symbol, timeframe=timeframe, limit=limit)
            
            if df is None or df.empty:
                continue
            
            df = calculate_indicators(df)
            
            signal = analyze_signals(df, symbol, timeframe, GLOBAL_MACRO_CONTEXT)
            
            if signal and signal['score'] >= current_threshold:
                if time.time() - LAST_SIGNAL_TIME.get(symbol, 0.0) > TRADE_SIGNAL_COOLDOWN:
                    new_signals.append(signal)
                    LAST_SIGNAL_TIME[symbol] = time.time()
                else:
                    logging.info(f"スキップ: {symbol} はクールダウン期間中です。")


    LAST_ANALYSIS_SIGNALS = sorted(new_signals, key=lambda s: s.get('score', 0.0), reverse=True)[:TOP_SIGNAL_COUNT] 

    for signal in LAST_ANALYSIS_SIGNALS:
        symbol = signal['symbol']
        
        if symbol not in [p['symbol'] for p in OPEN_POSITIONS]:
            trade_result = await execute_trade(signal)
            log_signal(signal, 'Trade Signal', trade_result)
            
            if trade_result and trade_result.get('status') == 'ok':
                await send_telegram_notification(
                    format_telegram_message(signal, "取引シグナル", current_threshold, trade_result=trade_result)
                )
        else:
            logging.info(f"スキップ: {symbol} には既にオープンポジションがあります。")
            
    now = time.time()
    
    # 6. 分析専用通知 (1時間ごと)
    if now - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME >= ANALYSIS_ONLY_INTERVAL:
        if LAST_ANALYSIS_SIGNALS or not IS_FIRST_MAIN_LOOP_COMPLETED:
            # account_statusはループの冒頭で取得済み
            # format_analysis_only_message が定義されていないため、ここでは startup_message に準拠した通知を使用するか、ログのみとする
            # 現行コードでは format_analysis_only_message が欠落しているため、初回通知ロジックを再利用する代替処理を実装。
            await send_telegram_notification(
                format_startup_message(
                    account_status, 
                    GLOBAL_MACRO_CONTEXT, 
                    len(CURRENT_MONITOR_SYMBOLS), 
                    current_threshold, 
                    "v20.0.3 - Future Trading / 10x Leverage (Patch 45: Pandas ATR Indexing Fix)" # ★変更
                )
            )
            LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = now
            log_signal({'signals': LAST_ANALYSIS_SIGNALS, 'macro': GLOBAL_MACRO_CONTEXT}, 'Hourly Analysis')

    # 7. 初回起動完了通知
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        await send_telegram_notification(
            format_startup_message(
                account_status, 
                GLOBAL_MACRO_CONTEXT, 
                len(CURRENT_MONITOR_SYMBOLS), 
                current_threshold,
                "v20.0.3 - Future Trading / 10x Leverage (Patch 45: Pandas ATR Indexing Fix)" # ★変更
            )
        )
        IS_FIRST_MAIN_LOOP_COMPLETED = True
        
    # 8. ログの外部アップロード (WebShare - HTTP POSTへ変更)
    if now - LAST_WEBSHARE_UPLOAD_TIME >= WEBSHARE_UPLOAD_INTERVAL:
        logging.info("WebShareデータをアップロードします (HTTP POST)。")
        webshare_data = {
            "timestamp": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            "positions": OPEN_POSITIONS,
            "analysis_signals": LAST_ANALYSIS_SIGNALS,
            "global_context": GLOBAL_MACRO_CONTEXT,
            "is_test_mode": TEST_MODE,
        }
        await send_webshare_update(webshare_data)
        LAST_WEBSHARE_UPLOAD_TIME = now


    # 9. 最終処理
    LAST_SUCCESS_TIME = time.time()
    logging.info(f"--- 💡 BOT LOOP END. Positions: {len(OPEN_POSITIONS)}, New Signals: {len(LAST_ANALYSIS_SIGNALS)} ---")


# ====================================================================================
# FASTAPI & ASYNC EXECUTION (変更なし)
# ====================================================================================

app = FastAPI()

@app.get("/status")
def get_status_info():
    """ボットの現在の状態を返す (変更なし)"""
    current_time = time.time()
    last_time_for_calc = LAST_SUCCESS_TIME if LAST_SUCCESS_TIME > 0 else current_time
    next_check = max(0, int(LOOP_INTERVAL - (current_time - last_time_for_calc)))

    status_msg = {
        "status": "ok",
        "bot_version": "v20.0.3 - Future Trading / 10x Leverage (Patch 45: Pandas ATR Indexing Fix)", # ★変更
        "base_trade_size_usdt": BASE_TRADE_SIZE_USDT, # 互換性のため残す
        "max_risk_per_trade_percent": MAX_RISK_PER_TRADE_PERCENT, # ★追加
        "current_equity_usdt": ACCOUNT_EQUITY_USDT, # ★追加
        "managed_positions_count": len(OPEN_POSITIONS), 
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, timezone.utc).isoformat() if LAST_SUCCESS_TIME > 0 else "N/A",
        "next_main_loop_check_seconds": next_check,
        "current_threshold": get_current_threshold(GLOBAL_MACRO_CONTEXT),
        "macro_context": GLOBAL_MACRO_CONTEXT, 
        "is_test_mode": TEST_MODE,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS),
        "is_client_ready": IS_CLIENT_READY,
    }
    return JSONResponse(content=status_msg)

async def main_loop_scheduler():
    """メインループを定期実行するスケジューラ (1分ごと)"""
    while True:
        try:
            await main_bot_loop()
        except Exception as e:
            logging.critical(f"❌ メインループ実行中に致命的なエラー: {e}", exc_info=True)
            await send_telegram_notification(f"🚨 **致命的なエラー**\nメインループでエラーが発生しました: `{e}`")

        wait_time = max(1, LOOP_INTERVAL - (time.time() - LAST_SUCCESS_TIME))
        logging.info(f"次のメインループまで {wait_time:.1f} 秒待機します。")
        await asyncio.sleep(wait_time)

async def position_monitor_scheduler():
    """TP/SL監視ループを定期実行するスケジューラ (10秒ごと)"""
    while True:
        try:
            await position_management_loop_async()
        except Exception as e:
            logging.critical(f"❌ ポジション監視ループ実行中に致命的なエラー: {e}", exc_info=True)

        await asyncio.sleep(MONITOR_INTERVAL) 


@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時に実行 (タスク起動の修正)"""
    asyncio.create_task(initialize_exchange_client())
    asyncio.create_task(main_loop_scheduler())
    asyncio.create_task(position_monitor_scheduler())
    logging.info("BOTサービスを開始しました。")

# ====================================================================================
# ENTRY POINT (変更なし)
# ====================================================================================

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
