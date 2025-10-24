# ====================================================================================
# Apex BOT v20.0.17 - Future Trading / 10x Leverage 
# (Patch 63: MEXC set_leverage FIX for Cross Margin)
#
# 改良・修正点:
# 1. 【バグ修正: Patch 63】MEXCでの set_leverage 呼び出し時に、クロスマージン ('openType': 2) を明示的に指定するように修正し、
#    set_leverage エラー ("mexc setLeverage() requires a positionId parameter or a symbol argument...") を解決。
# 2. 【ロジック維持: Patch 62】取引所シンボルID取得ロジックの修正を維持。
# 3. 【バグ修正: 本件】main_bot_loop内の CCXT fetch_ticker 呼び出しに await を追加し、
#    TypeError: 'coroutine' object is not subscriptable を解決。
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
# 12時間に修正 (7200 -> 43200) - Patch 61
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 12 
SIGNAL_THRESHOLD = 0.65             
TOP_SIGNAL_COUNT = 1                # ★ 常に1銘柄のみ取引試行 (Patch 59で導入)
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
    """
    if leverage <= 0 or entry_price <= 0:
        return 0.0
        
    # 必要証拠金率 (1 / Leverage)
    initial_margin_rate = 1 / leverage
    
    if side.lower() == 'long':
        # ロングの場合、価格下落で清算
        liquidation_price = entry_price * (1 - initial_margin_rate + maintenance_margin_rate)
    elif side.lower() == 'short':
        # ショートの場合、価格上昇で清算
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
    # ロジックの加点/減点要因は維持
    tech_data = signal.get('tech_data', {})
    timeframe = signal.get('timeframe', 'N/A')
    side = signal.get('side', 'long') # Long/Shortを判定
    
    LONG_TERM_REVERSAL_PENALTY_CONST = LONG_TERM_REVERSAL_PENALTY 
    MACD_CROSS_PENALTY_CONST = MACD_CROSS_PENALTY                 
    LIQUIDITY_BONUS_POINT_CONST = LIQUIDITY_BONUS_MAX           
    
    breakdown_list = []

    # 1. ベーススコア
    breakdown_list.append(f"  - **ベーススコア ({timeframe})**: <code>+{BASE_SCORE*100:.1f}</code> 点")
    
    # 2. 長期トレンド/構造の確認
    # ロング: SMA200のトレンド一致をチェック (Long Term Reversal Penalty回避)
    # ショート: SMA200のトレンド一致をチェック (Long Term Reversal Penaltyがボーナスに変わると解釈)
    
    penalty_value = tech_data.get('long_term_reversal_penalty_value', 0.0)
    
    if side == 'long':
        if penalty_value > 0.0:
            breakdown_list.append(f"  - ❌ 長期トレンド逆行 (SMA{LONG_TERM_SMA_LENGTH}): <code>-{penalty_value*100:.1f}</code> 点")
        else:
            breakdown_list.append(f"  - ✅ 長期トレンド一致 (SMA{LONG_TERM_SMA_LENGTH}): <code>+{LONG_TERM_REVERSAL_PENALTY_CONST*100:.1f}</code> 点 (ペナルティ回避)")
    else: # Short
        if penalty_value > 0.0:
            breakdown_list.append(f"  - ✅ 長期トレンド一致 (SMA{LONG_TERM_SMA_LENGTH}下): <code>+{penalty_value*100:.1f}</code> 点 (ロングペナルティ回避)")
        else:
            breakdown_list.append(f"  - ❌ 長期トレンド不利: <code>-{LONG_TERM_REVERSAL_PENALTY_CONST*100:.1f}</code> 点相当")

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
        # ロング: fgi_proxy > 0でボーナス、ショート: fgi_proxy < 0でボーナス
        is_fgi_favorable = (side == 'long' and fgi_bonus > 0) or (side == 'short' and fgi_bonus < 0)
        
        breakdown_list.append(f"  - {sign} FGIマクロ影響 ({side.upper()}方向): <code>{'+' if fgi_bonus > 0 else ''}{fgi_bonus*100:.1f}</code> 点")

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
        
    trade_status = "自動売買 **ON** (Long/Short)" if not TEST_MODE else "自動売買 **OFF** (TEST_MODE)" # ショート対応を追記

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
        balance_section += f"<pre>⚠️ ステータス取得失敗 (致命的エラーにより取引停止中)</pre>\n"
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
                side_tag = '🟢L' if pos.get('side', 'long') == 'long' else '🔴S' # ショート対応
                balance_section += f"    - Top {i+1}: {base_currency} ({side_tag}, SL: {format_price(pos['stop_loss'])} / TP: {format_price(pos['take_profit'])})\n"
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
    side = signal.get('side', 'long') # Long/Shortを判定
    
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
    
    # リスク幅、リワード幅の計算をLong/Shortで反転
    risk_width = abs(entry_price - stop_loss)
    reward_width = abs(take_profit - entry_price)

    if context == "取引シグナル":
        lot_size_units = signal.get('lot_size_units', 0.0) # 数量 (単位)
        notional_value = signal.get('notional_value', 0.0) # 名目価値
        
        trade_type_text = "先物ロング" if side == 'long' else "先物ショート"
        order_type_text = "成行買い" if side == 'long' else "成行売り"
        
        if TEST_MODE:
            trade_status_line = f"⚠️ **テストモード**: 取引は実行されません。(ロット: {format_usdt(notional_value)} USDT, {LEVERAGE}x)" 
        elif trade_result is None or trade_result.get('status') == 'error':
            trade_status_line = f"❌ **自動売買 失敗**: {trade_result.get('error_message', 'APIエラー')}"
        elif trade_result.get('status') == 'ok':
            trade_status_line = f"✅ **自動売買 成功**: **{trade_type_text}**注文を執行しました。" 
            
            filled_amount = trade_result.get('filled_amount', 0.0) 
            filled_usdt_notional = trade_result.get('filled_usdt', 0.0) 
            risk_usdt = signal.get('risk_usdt', 0.0) # リスク額
            
            trade_section = (
                f"💰 **取引実行結果**\n"
                f"  - **注文タイプ**: <code>先物 (Future) / {order_type_text} ({side.capitalize()})</code>\n" 
                f"  - **レバレッジ**: <code>{LEVERAGE}</code> 倍\n" 
                f"  - **リスク許容額**: <code>{format_usdt(risk_usdt)}</code> USDT ({MAX_RISK_PER_TRADE_PERCENT*100:.2f}%)\n" 
                f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
                f"  - **名目約定額**: <code>{format_usdt(filled_usdt_notional)}</code> USDT\n" 
            )
            
    elif context == "ポジション決済":
        exit_type_final = trade_result.get('exit_type', exit_type or '不明')
        side_text = "ロング" if side == 'long' else "ショート"
        trade_status_line = f"🔴 **{side_text} ポジション決済**: {exit_type_final} トリガー ({LEVERAGE}x)" 
        
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
        f"🚀 **Apex TRADE {context}** ({side.capitalize()})\n"
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
        f"  - **リスク幅 (SL)**: <code>{format_usdt(risk_width)}</code> USDT\n"
        f"  - **リワード幅 (TP)**: <code>{format_usdt(reward_width)}</code> USDT\n"
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
        
    message += (f"<i>Bot Ver: v20.0.17 - Future Trading / 10x Leverage (Patch 63: MEXC set_leverage FIX)</i>") # BOTバージョンを更新
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
            'side': data.get('side', 'N/A'), # ショート対応
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
    """CCXTクライアントを初期化し、市場情報をロードする"""
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    IS_CLIENT_READY = False
    
    if not API_KEY or not SECRET_KEY:
         logging.critical("❌ CCXT初期化スキップ: APIキー または SECRET_KEY が設定されていません。")
         return False
         
    # 💡 既存のクライアントがあれば、リソースを解放する
    if EXCHANGE_CLIENT:
        try:
            # 既存のクライアントを正常にクローズ
            await EXCHANGE_CLIENT.close()
            logging.info("✅ 既存のCCXTクライアントセッションを正常にクローズしました。")
        except Exception as e:
            # 競合状態や既にクローズされている場合にエラーになることがあるが、無視して続行
            logging.warning(f"⚠️ 既存クライアントのクローズ中にエラーが発生しましたが続行します: {e}")
        EXCHANGE_CLIENT = None
         
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

        # 💡 ネットワークタイムアウトを延長 (例: 30秒 = 30000ms)
        timeout_ms = 30000 
        
        EXCHANGE_CLIENT = exchange_class({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True,
            'options': options,
            'timeout': timeout_ms # ★ タイムアウト設定を追加
        })
        logging.info(f"✅ CCXTクライアントの初期化設定完了。リクエストタイムアウト: {timeout_ms/1000}秒。") 
        
        await EXCHANGE_CLIENT.load_markets() 
        
        # レバレッジの設定 (MEXC向け)
        if EXCHANGE_CLIENT.id == 'mexc':
            symbols_to_set_leverage = []
            # 💡 NOTE: load_marketsで全ての市場をロードしたため、ここでFuture/Swap市場を探してレバレッジを設定する
            for mkt in EXCHANGE_CLIENT.markets.values():
                 # USDT建てのSwap/Future市場を探す
                 # symbol: 'BTC/USDT:USDT' の形式を想定
                 if mkt['quote'] == 'USDT' and mkt['type'] in ['swap', 'future'] and mkt['active']:
                     # ユーザーが指定するCCXT標準のシンボル形式
                     symbols_to_set_leverage.append(mkt['symbol']) 
            
            for symbol in symbols_to_set_leverage:
                try:
                    # ★ Patch 63 FIX: MEXCのset_leverageエラーに対応するため、
                    #   'openType': 2 (Cross Margin) と 'positionType': 3 (Both sides - One-Way) を指定。
                    #   MEXCでは 'marginMode' の代わりに 'openType' を使用する。
                    
                    # 💡 set_leverage はシンボル形式 (BTC/USDT:USDT) で実行
                    await EXCHANGE_CLIENT.set_leverage(
                        LEVERAGE, 
                        symbol, 
                        params={
                            # 'cross' の設定を示す。openType: 1=isolated, 2=cross
                            'openType': 2, 
                            'positionType': 3, # 3: Both sides (一方向モードを維持)
                            # 'marginMode': 'cross' # CCXT標準だが、MEXC APIが直接要求しないため削除
                        }
                    ) 
                except Exception as e:
                    logging.warning(f"⚠️ {symbol} のレバレッジ設定 ({LEVERAGE}x) に失敗しました: {e}")
            
            logging.info(f"✅ MEXCのレバレッジを主要な先物銘柄 ({len(symbols_to_set_leverage)}件) で {LEVERAGE}x (クロス) に設定しました。")
        
        # ログメッセージを 'future' モードに変更
        logging.info(f"✅ CCXTクライアント ({CCXT_CLIENT_NAME}) を先物取引モードで初期化し、市場情報をロードしました。") 
        IS_CLIENT_READY = True
        return True
    except ccxt.AuthenticationError as e:
        logging.critical(f"❌ CCXT初期化失敗 - 認証エラー: APIキー/シークレットを確認してください。{e}", exc_info=True)
    except ccxt.ExchangeNotAvailable as e:
        logging.critical(f"❌ CCXT初期化失敗 - 取引所接続エラー: サーバーが利用できません。{e}", exc_info=True)
    except ccxt.NetworkError as e:
        # RequestTimeoutもccxt.NetworkErrorを継承しているため、ここで捕捉
        logging.critical(f"❌ CCXT初期化失敗 - ネットワークエラー/タイムアウト: 接続を確認してください。{e}", exc_info=True)
    except Exception as e:
        # Patch 57で RuntimeError: Session is closed の原因となる競合状態を避けるため、このクリティカルログは維持
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
        balance = None 
        if EXCHANGE_CLIENT.id == 'mexc': 
            # MEXC先物では defaultType='swap' が使われることが多い 
            logging.info("ℹ️ MEXC: fetch_balance(type='swap') を使用して口座情報を取得します。") 
            balance = await EXCHANGE_CLIENT.fetch_balance(params={'defaultType': 'swap'}) 
        else: 
            # 他の取引所向け 
            fetch_params = {'type': 'future'} if TRADE_TYPE == 'future' else {} 
            balance = await EXCHANGE_CLIENT.fetch_balance(params=fetch_params) 
        
        # balanceが取得できなかった場合はエラー 
        if not balance: 
            raise Exception("Balance object is empty.") 
        
        # 1. total_usdt_balance (総資産: Equity) の取得 (標準フィールド) 
        total_usdt_balance = balance.get('total', {}).get('USDT', 0.0) 
        
        # 2. MEXC特有のフォールバックロジック (infoからtotalEquityを探す) 
        if EXCHANGE_CLIENT.id == 'mexc' and balance.get('info'): 
            raw_data = balance['info'] 
            mexc_raw_data = None 
            # raw_dataが辞書であり、'data'キーを持つ場合 
            if isinstance(raw_data, dict) and 'data' in raw_data: 
                mexc_raw_data = raw_data.get('data') 
            else: 
                # raw_data自体がリストの場合 
                mexc_raw_data = raw_data 
            
            mexc_data: Optional[Dict] = None 
            if isinstance(mexc_raw_data, list) and len(mexc_raw_data) > 0: 
                # リストの場合、通常は最初の要素にUSDTの要約情報が含まれると想定 
                if isinstance(mexc_raw_data[0], dict): 
                    mexc_data = mexc_raw_data[0] 
            elif isinstance(mexc_raw_data, dict):
                 mexc_data = mexc_raw_data
            
            # mexc_data (dictを期待) の中から totalEquity を抽出するロジック 
            if mexc_data: 
                total_usdt_balance_fallback = 0.0 
                # Case A: V3 API形式 - mexc_data自体がUSDT資産情報を持っている 
                if mexc_data.get('currency') == 'USDT': 
                    total_usdt_balance_fallback = float(mexc_data.get('totalEquity', 0.0)) 
                # Case B: V1 API形式 - mexc_data内の'assets'リストに情報がある 
                elif mexc_data.get('assets') and isinstance(mexc_data['assets'], list): 
                    for asset in mexc_data['assets']: 
                        if asset.get('currency') == 'USDT': 
                            total_usdt_balance_fallback = float(asset.get('totalEquity', 0.0)) 
                            break 
                
                if total_usdt_balance_fallback > 0: 
                    # フォールバックで取得できた値を採用 
                    total_usdt_balance = total_usdt_balance_fallback 
                    logging.warning("⚠️ MEXC専用フォールバックロジックで Equity を取得しました。") 
        
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
        logging.critical(f"❌ 口座ステータス取得失敗 (認証エラー): APIキー/シークレットを確認してください。{e}") 
    except Exception as e: 
        logging.error(f"❌ 口座ステータス取得失敗 (予期せぬエラー): {e}")
        
    ACCOUNT_EQUITY_USDT = 0.0
    return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}


async def fetch_open_positions() -> List[Dict]:
    """現在保有中のオープンポジションをCCXTから取得し、グローバル変数に格納する。"""
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ ポジション取得失敗: CCXTクライアントが準備できていません。")
        return []

    try:
        # fetch_positions は先物ポジションを取得
        positions = await EXCHANGE_CLIENT.fetch_positions()
        
        # ボットが管理しやすい形式にフィルタリング・整形
        managed_positions = []
        for pos in positions:
            if pos['entryPrice'] and pos['notional'] != 0: # 建値があり、名目価値が0でないもの
                # ccxtのpos['side']は 'long' または 'short'
                
                # ポジションの必須フィールドを抽出 (SL/TPは後で追加される)
                managed_positions.append({
                    'symbol': pos['symbol'],
                    'id': pos['id'] if 'id' in pos else str(uuid.uuid4()), # IDがない場合のフォールバック
                    'side': pos['side'],
                    'entry_price': pos['entryPrice'],
                    'filled_amount': abs(pos['contracts']), # 契約数を絶対値で
                    'filled_usdt': abs(pos['notional']), # 名目価値を絶対値で
                    'stop_loss': 0.0, # 初期値
                    'take_profit': 0.0, # 初期値
                    'pnl_usdt': pos['unrealizedPnl'],
                })

        # 以前の OPEN_POSITIONS に SL/TP 情報があればマージする
        for new_pos in managed_positions:
            for old_pos in OPEN_POSITIONS:
                if new_pos['symbol'] == old_pos['symbol'] and new_pos['side'] == old_pos['side']:
                    # SL/TP情報はボットが設定したものを使用
                    new_pos['stop_loss'] = old_pos.get('stop_loss', 0.0)
                    new_pos['take_profit'] = old_pos.get('take_profit', 0.0)
                    new_pos['liquidation_price'] = old_pos.get('liquidation_price', 0.0)
                    break
        
        OPEN_POSITIONS = managed_positions
        logging.info(f"✅ CCXTから最新のオープンポジション情報を取得しました (現在 {len(OPEN_POSITIONS)} 銘柄)。")
        return OPEN_POSITIONS

    except ccxt.NetworkError as e:
        logging.error(f"❌ ポジション取得失敗 (ネットワークエラー): {e}")
    except ccxt.NotSupported as e:
         # 一部の取引所では fetch_positions がサポートされていない場合があるため、その場合は空リストを返す
         logging.warning(f"⚠️ fetch_positions がサポートされていません ({EXCHANGE_CLIENT.id})。手動でポジションを管理する必要があります。")
    except Exception as e:
        logging.error(f"❌ ポジション取得失敗 (予期せぬエラー): {e}")
    
    return []

# ====================================================================================
# MARKET DATA ANALYSIS
# ====================================================================================

async def _fetch_ohlcv_with_retry(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """OHLCVデータを取得する際のネットワークエラーをリトライするヘルパー関数"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return None

    for attempt in range(3): # 最大3回リトライ
        try:
            # CCXTのfetch_ohlcvは非同期メソッドなのでawait
            ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
            if not ohlcv or len(ohlcv) < limit:
                logging.warning(f"⚠️ {symbol} {timeframe}: データ数が不足しています (取得数: {len(ohlcv) if ohlcv else 0}/{limit})。")
                # データ不足も失敗とみなす (指標計算に影響するため)
                return None 

            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
            
            return df

        except (ccxt.NetworkError, ccxt.ExchangeNotAvailable) as e:
            logging.warning(f"⚠️ {symbol} {timeframe}: データ取得ネットワークエラー (試行 {attempt + 1}/3): {e}")
            await asyncio.sleep(1 + attempt * 2) # 待機時間を延長
        except Exception as e:
            logging.error(f"❌ {symbol} {timeframe}: データ取得予期せぬエラー: {e}")
            return None

    logging.error(f"❌ {symbol} {timeframe}: データ取得リトライ失敗。この銘柄をスキップします。")
    return None

def calculate_indicators(df: pd.DataFrame, timeframe: str) -> Optional[pd.DataFrame]:
    """Pandas-TAを使用してテクニカル指標を計算する"""
    if df.empty or len(df) < LONG_TERM_SMA_LENGTH: 
        return None

    try:
        # 移動平均 (SMA)
        df.ta.sma(length=20, append=True)
        df.ta.sma(length=50, append=True)
        df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True) # SMA200

        # RSI
        df.ta.rsi(length=14, append=True)

        # MACD
        df.ta.macd(append=True)

        # ATR (ボラティリティ)
        df.ta.atr(length=ATR_LENGTH, append=True)

        # ボリンジャーバンド (BBANDS)
        df.ta.bbands(append=True)

        # OBV (出来高モメンタム)
        df.ta.obv(append=True)

        return df
    except Exception as e:
        logging.error(f"❌ {timeframe}のテクニカル指標計算中にエラー: {e}")
        return None

def generate_signal_score(df: pd.DataFrame, timeframe: str, symbol: str, current_price: float, macro_context: Dict) -> List[Dict]:
    """
    テクニカル分析の結果に基づき、ロング/ショートのシグナルスコアを計算する。
    1つの時間足でLong/Short両方のスコアを返す可能性がある。
    """
    signals = []
    
    # 最新のローソク足データを取得
    last_row = df.iloc[-1]
    
    # 共通のベースライン
    base_score = BASE_SCORE
    
    # テクニカルデータ辞書を初期化
    tech_data = {
        'timeframe': timeframe,
        'long_term_reversal_penalty_value': 0.0,
        'structural_pivot_bonus': 0.0,
        'macd_penalty_value': 0.0,
        'liquidity_bonus_value': 0.0,
        'obv_momentum_bonus_value': 0.0,
        'volatility_penalty_value': 0.0,
        'sentiment_fgi_proxy_bonus': 0.0,
        'forex_bonus': 0.0,
    }

    # 1. 長期トレンドと構造の確認 (SMA200)
    sma200 = last_row[f'SMA_{LONG_TERM_SMA_LENGTH}']
    
    # ロングシグナルスコア計算
    score_long = base_score
    
    # a. 長期トレンド逆行ペナルティ/ボーナス
    if current_price < sma200: # 現在価格がSMA200より下
        # 逆行ペナルティ
        score_long -= LONG_TERM_REVERSAL_PENALTY
        tech_data['long_term_reversal_penalty_value'] = LONG_TERM_REVERSAL_PENALTY
    else:
        # トレンド一致ボーナス (ペナルティ回避)
        score_long += LONG_TERM_REVERSAL_PENALTY   # LONG_TERM_REVERSAL_PENALTY_CONST はグローバル定数 (0.20)

    # b. モメンタム/クロス (RSI/MACD)
    rsi = last_row['RSI_14']
    macd_hist = last_row['MACDh_12_26_9']
    
    is_macd_cross_down = macd_hist < 0 # MACDヒストグラムがマイナス
    is_rsi_weak = rsi < RSI_MOMENTUM_LOW # RSIが弱い
    
    if is_macd_cross_down or is_rsi_weak:
        # モメンタム不利ペナルティ
        score_long -= MACD_CROSS_PENALTY
        tech_data['macd_penalty_value'] = MACD_CROSS_PENALTY
        
    # c. 出来高/OBV確証ボーナス
    # OBVが最新のバーで増加しているか (ロングの場合)
    if len(df) >= 2:
        if last_row['OBV'] > df.iloc[-2]['OBV']:
            score_long += OBV_MOMENTUM_BONUS
            tech_data['obv_momentum_bonus_value'] = OBV_MOMENTUM_BONUS
            
    # d. 価格構造/ピボット支持ボーナス (シンプル化: 直近安値付近であればボーナス)
    low_price = last_row['low']
    window_low = df.iloc[-10:-1]['low'].min() if len(df) >= 10 else low_price
    
    # 現在価格が直近10バーの最安値から1%以内
    if window_low > 0 and (current_price - window_low) / window_low < 0.01: 
        score_long += STRUCTURAL_PIVOT_BONUS
        tech_data['structural_pivot_bonus'] = STRUCTURAL_PIVOT_BONUS
        
    # e. ボラティリティ過熱ペナルティ (ボリンジャーバンドの上限超え)
    bb_upper = last_row['BBU_5_2.0']
    
    # 価格がBB上限を超えている場合、反転リスクでペナルティ
    if current_price > bb_upper:
        score_long -= VOLATILITY_BB_PENALTY_THRESHOLD
        tech_data['volatility_penalty_value'] = -VOLATILITY_BB_PENALTY_THRESHOLD

    # f. 流動性/マクロ要因
    # 流動性ボーナス: ボリュームが過去平均より高い場合にボーナス
    # (ここでは簡略化のため、常に最大ボーナスを付与するロジックを維持)
    score_long += LIQUIDITY_BONUS_MAX
    tech_data['liquidity_bonus_value'] = LIQUIDITY_BONUS_MAX
    
    # g. FGIマクロ要因ボーナス (ロングはFGIがプラスでボーナス)
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    
    fgi_bonus_long = min(FGI_PROXY_BONUS_MAX, max(0.0, fgi_proxy)) # FGIがプラスの場合のみ加点
    score_long += fgi_bonus_long
    tech_data['sentiment_fgi_proxy_bonus'] = fgi_bonus_long
    
    # 為替ボーナス (現在は機能削除済)
    score_long += forex_bonus
    tech_data['forex_bonus'] = forex_bonus

    # ロングシグナルとして追加
    signals.append({
        'symbol': symbol,
        'timeframe': timeframe,
        'side': 'long',
        'score': score_long,
        'current_price': current_price,
        'tech_data': tech_data.copy(),
        'is_tradable': True,
        'cooldown_remaining': 0
    })

    
    # ショートシグナルスコア計算 (ロジックを反転)
    score_short = base_score
    tech_data_short = tech_data.copy()

    # a. 長期トレンド逆行ペナルティ/ボーナス
    if current_price > sma200: # 現在価格がSMA200より上 (ショートにとって逆行)
        score_short -= LONG_TERM_REVERSAL_PENALTY
        tech_data_short['long_term_reversal_penalty_value'] = LONG_TERM_REVERSAL_PENALTY
    else:
        # トレンド一致ボーナス (ペナルティ回避)
        score_short += LONG_TERM_REVERSAL_PENALTY 
        
    # b. モメンタム/クロス (RSI/MACD)
    is_macd_cross_up = macd_hist > 0 # MACDヒストグラムがプラス
    is_rsi_overbought = rsi > (100 - RSI_MOMENTUM_LOW) # RSIが強い（買われすぎ）
    
    if is_macd_cross_up or not is_rsi_overbought:
        # モメンタム不利ペナルティ
        score_short -= MACD_CROSS_PENALTY
        tech_data_short['macd_penalty_value'] = MACD_CROSS_PENALTY

    # c. 出来高/OBV確証ボーナス
    # OBVが最新のバーで減少しているか (ショートの場合)
    if len(df) >= 2:
        if last_row['OBV'] < df.iloc[-2]['OBV']:
            score_short += OBV_MOMENTUM_BONUS
            tech_data_short['obv_momentum_bonus_value'] = OBV_MOMENTUM_BONUS

    # d. 価格構造/ピボット支持ボーナス (シンプル化: 直近高値付近であればボーナス)
    high_price = last_row['high']
    window_high = df.iloc[-10:-1]['high'].max() if len(df) >= 10 else high_price

    # 現在価格が直近10バーの最高値から1%以内
    if window_high > 0 and (window_high - current_price) / window_high < 0.01:
        score_short += STRUCTURAL_PIVOT_BONUS
        tech_data_short['structural_pivot_bonus'] = STRUCTURAL_PIVOT_BONUS

    # e. ボラティリティ過熱ペナルティ (ボリンジャーバンドの下限超え)
    bb_lower = last_row['BBL_5_2.0']
    
    # 価格がBB下限を下回っている場合、反転リスクでペナルティ
    if current_price < bb_lower:
        score_short -= VOLATILITY_BB_PENALTY_THRESHOLD
        tech_data_short['volatility_penalty_value'] = -VOLATILITY_BB_PENALTY_THRESHOLD

    # f. 流動性/マクロ要因 (ロングと同様に常に最大ボーナスを付与するロジックを維持)
    score_short += LIQUIDITY_BONUS_MAX
    tech_data_short['liquidity_bonus_value'] = LIQUIDITY_BONUS_MAX
    
    # g. FGIマクロ要因ボーナス (ショートはFGIがマイナスでボーナス)
    fgi_bonus_short = min(FGI_PROXY_BONUS_MAX, max(0.0, -fgi_proxy)) # FGIがマイナスの場合のみ加点
    score_short += fgi_bonus_short
    tech_data_short['sentiment_fgi_proxy_bonus'] = fgi_bonus_short
    
    # 為替ボーナス (現在は機能削除済)
    score_short += forex_bonus
    tech_data_short['forex_bonus'] = forex_bonus

    # ショートシグナルとして追加
    signals.append({
        'symbol': symbol,
        'timeframe': timeframe,
        'side': 'short',
        'score': score_short,
        'current_price': current_price,
        'tech_data': tech_data_short.copy(),
        'is_tradable': True,
        'cooldown_remaining': 0
    })

    return signals


async def fetch_top_symbols(limit: int) -> List[str]:
    """24時間出来高に基づき、上位のシンボルを動的に更新する (BTC/ETH/主要銘柄は常に維持)"""
    global EXCHANGE_CLIENT
    
    if SKIP_MARKET_UPDATE:
        logging.warning("⚠️ SKIP_MARKET_UPDATEがTrueに設定されています。監視銘柄の更新をスキップし、固定リストを使用します。")
        return DEFAULT_SYMBOLS.copy()

    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 出来高TOP銘柄取得失敗: CCXTクライアントが準備できていません。固定リストを使用します。")
        return DEFAULT_SYMBOLS.copy()

    try:
        # fetch_tickersで全銘柄の最新ティッカー情報を取得
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        # USDT建ての先物/スワップ銘柄をフィルタリング
        future_tickers = []
        for symbol, ticker in tickers.items():
            # CCXT標準のシンボル形式 'BTC/USDT:USDT' を想定
            if 'USDT' in symbol and (ticker.get('info', {}).get('openType') == 1 or ticker.get('info', {}).get('openType') == 2 or ticker.get('type') == 'swap' or ticker.get('type') == 'future'):
                 # 24時間取引量 (quoteVolume: USDT建てのボリューム) でソートする
                 if ticker and ticker.get('quoteVolume') is not None:
                     future_tickers.append(ticker)

        # quoteVolume (USDT建て出来高) で降順ソート
        future_tickers.sort(key=lambda x: x['quoteVolume'] or 0, reverse=True)

        # TOP N 銘柄を選択
        top_n_symbols = [t['symbol'] for t in future_tickers if t['symbol'] not in DEFAULT_SYMBOLS][:limit]
        
        # 既存のDEFAULT_SYMBOLSとマージし、重複を排除
        new_monitor_symbols = list(set(DEFAULT_SYMBOLS + top_n_symbols))
        
        logging.info(f"✅ 出来高TOP ({limit}件) を取得し、監視銘柄を {len(new_monitor_symbols)} 件に更新しました。")
        return new_monitor_symbols

    except Exception as e:
        logging.error(f"❌ 出来高TOP銘柄取得中にエラー: {e}。固定リストを使用します。")
        return DEFAULT_SYMBOLS.copy()

async def fetch_macro_context() -> Dict:
    """外部APIからFGI (恐怖・貪欲指数) などのマクロ情報を取得する"""
    fgi_raw_value = GLOBAL_MACRO_CONTEXT.get('fgi_raw_value', 'N/A')
    fgi_proxy = GLOBAL_MACRO_CONTEXT.get('fgi_proxy', 0.0)

    # FGI (Fear & Greed Index)
    fgi_url = "https://api.alternative.me/fng/?limit=1"
    try:
        response = await asyncio.to_thread(requests.get, fgi_url, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        if data and data.get('data'):
            latest_fgi = data['data'][0]
            fgi_value = int(latest_fgi.get('value', 50))
            fgi_raw_value = latest_fgi.get('value_classification', 'Neutral')
            
            # FGI Proxy: -0.5 (Extreme Fear) to +0.5 (Extreme Greed)
            # FGI: 0 to 100
            fgi_proxy = (fgi_value - 50) / 100.0 
            
            logging.info(f"✅ FGIマクロ情報を更新しました: {fgi_raw_value} (Proxy: {fgi_proxy:.2f})")
            
    except requests.exceptions.RequestException as e:
        logging.warning(f"⚠️ FGI取得に失敗: {e}。前回の値 (Proxy: {fgi_proxy:.2f}) を維持します。")

    return {
        'fgi_proxy': fgi_proxy,
        'fgi_raw_value': fgi_raw_value,
        'forex_bonus': 0.0 # 機能削除済みの為替ボーナスは0.0を維持
    }


async def analyze_market_data_async(monitor_symbols: List[str], macro_context: Dict) -> List[Dict]:
    """
    非同期で複数の銘柄・時間足のデータを取得・分析し、シグナルを生成する。
    """
    logging.info("⚙️ 市場データ分析を開始します。")
    
    tasks = []
    # 銘柄と時間足の組み合わせごとにタスクを作成
    for symbol in monitor_symbols:
        for timeframe in TARGET_TIMEFRAMES:
            # ポジションを保有している銘柄はスキップしない (再分析して継続取引の判断に使うため)
            
            # 既に直近12時間以内に取引シグナルが出ている銘柄は、その時間足での分析をスキップ
            last_time = LAST_SIGNAL_TIME.get(f"{symbol}_{timeframe}", 0)
            if time.time() - last_time < TRADE_SIGNAL_COOLDOWN:
                cooldown_remaining = int(TRADE_SIGNAL_COOLDOWN - (time.time() - last_time))
                tasks.append(asyncio.create_task(
                    asyncio.sleep(0, result={'symbol': symbol, 'timeframe': timeframe, 'side': 'N/A', 'score': 0.0, 'is_tradable': False, 'cooldown_remaining': cooldown_remaining})
                ))
                continue
                
            tasks.append(asyncio.create_task(
                _analyze_single_pair_timeframe(symbol, timeframe, macro_context)
            ))
            
    results = await asyncio.gather(*tasks)
    
    all_signals = []
    for result in results:
        if isinstance(result, list):
            all_signals.extend(result)
            
    # スコアで降順ソート
    all_signals.sort(key=lambda x: x['score'], reverse=True)
    
    logging.info(f"✅ 全銘柄・全時間足の分析を完了しました。合計 {len(all_signals)} 件のシグナル候補。")
    return all_signals

async def _analyze_single_pair_timeframe(symbol: str, timeframe: str, macro_context: Dict) -> List[Dict]:
    """単一の銘柄と時間足のデータを取得・分析する"""
    required_limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 1000)
    
    # 1. OHLCVデータ取得
    df = await _fetch_ohlcv_with_retry(symbol, timeframe, required_limit)
    if df is None:
        return []

    # 2. テクニカル指標計算
    df = calculate_indicators(df, timeframe)
    if df is None:
        return []

    # 3. 現在価格取得 (最新のローソク足の終値を使用)
    current_price = df['close'].iloc[-1]

    # 4. シグナルスコア生成
    signals = generate_signal_score(df, timeframe, symbol, current_price, macro_context)
    
    # 5. リスクリワード比率 (RRR) と SL/TP の計算
    for signal in signals:
        side = signal['side']
        score = signal['score']
        
        # ATRベースのボラティリティ取得
        atr = df[f'ATR_{ATR_LENGTH}'].iloc[-1]
        
        # リスク許容幅 (ATR * 乗数、または最小リスクパーセンテージ)
        risk_usdt_percent = atr * ATR_MULTIPLIER_SL # ATRベースの幅
        min_risk_usdt_percent = current_price * MIN_RISK_PERCENT # 最小リスクパーセンテージ
        
        # SL幅は、(ATRベースの幅) または (最小リスクパーセンテージ) の大きい方を採用
        sl_range = max(risk_usdt_percent, min_risk_usdt_percent)

        # TP幅: スコアに応じて動的に設定 (例: RRRを1.5から3.0の間で設定)
        # スコアが高くなるほど、RRRを大きくする
        # 例: 0.65 (閾値) -> RRR 1.5, 0.90 -> RRR 3.0
        # 0.65未満は取引しないので、正規化して1.5+αとする
        
        # 閾値からの差分を正規化 (0.0～1.0)
        min_threshold = get_current_threshold(macro_context)
        max_score = 1.0 
        
        # スコアが閾値未満の場合は RRR = 0.0 に設定 (取引対象外)
        if score < min_threshold:
            rr_ratio = 0.0
        else:
            # 閾値(min_threshold)から1.0の間を0.0から1.0に正規化
            normalized_score = (score - min_threshold) / (max_score - min_threshold)
            normalized_score = max(0.0, min(1.0, normalized_score))
            
            # RRRを 1.5 (normalized_score=0.0) から 3.0 (normalized_score=1.0) の間で設定
            rr_ratio = 1.5 + (normalized_score * 1.5)
            rr_ratio = round(rr_ratio, 2)
            
        signal['rr_ratio'] = rr_ratio
        
        if side == 'long':
            stop_loss = current_price - sl_range
            take_profit = current_price + (sl_range * rr_ratio)
        else: # short
            stop_loss = current_price + sl_range
            take_profit = current_price - (sl_range * rr_ratio)
            
        signal['stop_loss'] = stop_loss
        signal['take_profit'] = take_profit
        
        # 清算価格を計算
        signal['liquidation_price'] = calculate_liquidation_price(
            current_price, 
            LEVERAGE, 
            side, 
            MIN_MAINTENANCE_MARGIN_RATE
        )
        
        # 取引可能かどうかを最終チェック
        if rr_ratio < 1.0 or signal['liquidation_price'] <= 0.0:
            signal['is_tradable'] = False
        
    return [s for s in signals if s['is_tradable']] # 取引可能なシグナルのみを返す


async def trade_execution_logic(signal: Dict) -> Optional[Dict]:
    """
    リスクベースサイジングで注文数量を計算し、市場価格で取引を実行する。
    SL/TP注文はボット側で監視する。
    """
    global EXCHANGE_CLIENT, ACCOUNT_EQUITY_USDT
    symbol = signal['symbol']
    side = signal['side']
    entry_price = signal['current_price']
    stop_loss = signal['stop_loss']
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'CCXTクライアントが準備できていません。'}
        
    if ACCOUNT_EQUITY_USDT <= 0:
        return {'status': 'error', 'error_message': '口座残高 (Equity) がゼロまたは取得できていません。'}
        
    # 1. リスク許容額の計算 (取引所がレバレッジを管理するため、ここではリスク額を計算)
    risk_usdt = ACCOUNT_EQUITY_USDT * MAX_RISK_PER_TRADE_PERCENT
    signal['risk_usdt'] = risk_usdt
    
    # 2. 許容される価格変動幅 (USDT建て)
    # SL/TPの幅はUSDT建て価格で計算される
    price_diff_to_sl = abs(entry_price - stop_loss)
    
    if price_diff_to_sl <= 0:
        return {'status': 'error', 'error_message': 'SL幅がゼロです。取引をスキップします。'}
        
    # 3. 許容される名目注文額 (Notional Value) の計算
    # Notional Value (USD) = (Risk Amount / Percentage to SL) * Leverage
    # Percentage to SL = price_diff_to_sl / entry_price
    # Notional_Value = Risk_Amount / Percentage_Loss
    
    # 許容される最大名目損失率 (レバレッジ考慮前のマージンベース)
    max_loss_percentage = price_diff_to_sl / entry_price 
    
    # 許容される名目注文額 (最大損失率がリスク額に相当するロットサイズ)
    # lot_usdt = risk_usdt / (max_loss_percentage * (1 / LEVERAGE))
    # Note: CCXT/MEXCはレバレッジを考慮して自動で証拠金を計算するため、シンプルに以下を使用。
    lot_usdt = risk_usdt / max_loss_percentage
    
    # 4. 注文数量の計算 (単位: ベース通貨)
    # 注文数量 (Amount) = Notional Value (USDT) / Entry Price (USDT)
    order_amount = lot_usdt / entry_price
    
    # 5. 取引所の最小/最大数量、最小ティックサイズに合わせて調整
    market = EXCHANGE_CLIENT.markets.get(symbol)
    if not market:
        return {'status': 'error', 'error_message': f'取引所市場情報が {symbol} で見つかりません。'}
        
    # 数量を取引所のステップサイズで丸める
    precision = market['precision']
    
    # amount_precision = precision.get('amount', 4)
    # price_precision = precision.get('price', 2) 
    
    # CCXTのamountToLots/amountToPrecisionを使用して丸める (CCXT標準機能に依存)
    # CCXTは `amountToPrecision` を提供することが多い
    if hasattr(EXCHANGE_CLIENT, 'amount_to_precision'):
        try:
            order_amount = float(EXCHANGE_CLIENT.amount_to_precision(symbol, order_amount))
        except Exception as e:
            logging.error(f"❌ 数量の精度調整失敗: {e}。丸め処理をスキップします。")
            order_amount = round(order_amount, 4) # フォールバック

    # 最小注文数量のチェック (MEXCの最小名目価値は約5 USDT)
    min_notional = market.get('limits', {}).get('cost', {}).get('min', 5.0) 
    current_notional = order_amount * entry_price
    
    if current_notional < min_notional:
        # 最小ロットを下回る場合は、取引をスキップするか、最小ロットに合わせる
        # ここではリスク管理を優先し、スキップする
        logging.warning(f"⚠️ {symbol}: 計算された名目額 ({format_usdt(current_notional)} USDT) が最小名目額 ({min_notional} USDT) を下回ります。取引をスキップします。")
        return {'status': 'error', 'error_message': f'名目注文額が小さすぎます ({format_usdt(current_notional)} USDT)。'}

    signal['lot_size_units'] = order_amount
    signal['notional_value'] = current_notional
    
    # 6. 取引実行
    if TEST_MODE:
        logging.warning(f"⚠️ TEST_MODE: {symbol} ({side.upper()}) の取引 (ロット: {format_usdt(current_notional)} USDT) は実行されません。")
        return {'status': 'ok', 'filled_amount': order_amount, 'filled_usdt': current_notional, 'entry_price': entry_price}

    order_type = 'market'
    side_ccxt = 'buy' if side == 'long' else 'sell'
    
    try:
        # 成行注文を執行 (CCXTのcreate_orderは非同期メソッドなのでawait)
        order = await EXCHANGE_CLIENT.create_order(
            symbol, 
            order_type, 
            side_ccxt, 
            order_amount, 
            params={
                # 先物取引のレバレッジ、マージンモードをCCXTに任せる
                'leverage': LEVERAGE, 
                'marginMode': 'cross' # Cross Margin mode
            }
        )
        
        # 注文が約定したかどうかを確認 (成行なのでほぼ即座に約定)
        if order.get('status') == 'closed' or order.get('filled') > 0:
            filled_amount = order.get('filled')
            filled_price = order.get('average') or entry_price
            
            # 注文成功ログ
            logging.info(f"✅ {symbol}: {side_ccxt.upper()} {format_usdt(filled_amount)} 注文成功。約定価格: {format_price(filled_price)}")
            
            # 成功したポジションを OPEN_POSITIONS に即座に追加 (SL/TP監視用)
            OPEN_POSITIONS.append({
                'symbol': symbol,
                'id': order.get('id', str(uuid.uuid4())),
                'side': side,
                'entry_price': filled_price,
                'filled_amount': filled_amount,
                'filled_usdt': filled_amount * filled_price,
                'stop_loss': stop_loss,
                'take_profit': signal['take_profit'],
                'liquidation_price': signal['liquidation_price'],
                'pnl_usdt': 0.0 # 初期値
            })
            
            # シグナルに成功情報を付加
            signal['trade_result'] = {
                'status': 'ok',
                'filled_amount': filled_amount,
                'filled_usdt': filled_amount * filled_price,
                'entry_price': filled_price
            }
            
            return signal['trade_result']
            
        else:
            raise Exception(f"注文は執行されましたが、約定が完了しませんでした。ステータス: {order.get('status')}")

    except ccxt.ExchangeError as e:
        error_msg = f"取引所APIエラー: {e}"
        logging.error(f"❌ {symbol} 取引失敗: {error_msg}")
        return {'status': 'error', 'error_message': error_msg}
    except ccxt.NetworkError as e:
        error_msg = f"ネットワークエラー: {e}"
        logging.error(f"❌ {symbol} 取引失敗: {error_msg}")
        return {'status': 'error', 'error_message': error_msg}
    except Exception as e:
        error_msg = f"予期せぬエラー: {e}"
        logging.error(f"❌ {symbol} 取引失敗: {error_msg}")
        return {'status': 'error', 'error_message': error_msg}


async def close_position(position: Dict, exit_type: str, current_price: float) -> Optional[Dict]:
    """オープンポジションを市場価格で決済する"""
    global EXCHANGE_CLIENT
    symbol = position['symbol']
    side = position['side']
    amount = position['filled_amount']
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'CCXTクライアントが準備できていません。'}
        
    if TEST_MODE:
        logging.warning(f"⚠️ TEST_MODE: {symbol} ({side.upper()}) の決済 ({exit_type}) は実行されません。")
        # PNLを計算して成功したフリをする
        pnl_usdt = (current_price - position['entry_price']) * amount * (1 if side == 'long' else -1)
        pnl_rate = (pnl_usdt / (position['filled_usdt'] / LEVERAGE))
        return {
            'status': 'ok', 
            'exit_price': current_price, 
            'entry_price': position['entry_price'],
            'filled_amount': amount, 
            'pnl_usdt': pnl_usdt,
            'pnl_rate': pnl_rate,
            'exit_type': exit_type
        }
    
    order_type = 'market'
    # ロングを決済 -> 'sell'、ショートを決済 -> 'buy'
    side_ccxt = 'sell' if side == 'long' else 'buy'
    
    try:
        # 成行注文を執行 (CCXTのcreate_orderは非同期メソッドなのでawait)
        order = await EXCHANGE_CLIENT.create_order(
            symbol, 
            order_type, 
            side_ccxt, 
            amount,
            params={
                # 決済注文であることを明示的に示すパラメーターがあれば追加 (例: MEXCでは'reduceOnly': True)
                'reduceOnly': True 
            }
        )
        
        # 注文が約定したかどうかを確認
        if order.get('status') == 'closed' or order.get('filled') >= amount * 0.99: # ほぼ全量決済
            filled_price = order.get('average') or current_price
            
            # 損益計算
            pnl_usdt = (filled_price - position['entry_price']) * amount * (1 if side == 'long' else -1)
            # 証拠金に対する損益率 (Rough PnL Rate)
            pnl_rate = (pnl_usdt / (position['filled_usdt'] / LEVERAGE)) if position['filled_usdt'] > 0 else 0.0

            logging.info(f"✅ {symbol}: {side.upper()} ポジションを {exit_type} で決済成功。決済価格: {format_price(filled_price)}")
            
            return {
                'status': 'ok',
                'exit_price': filled_price,
                'entry_price': position['entry_price'],
                'filled_amount': amount,
                'pnl_usdt': pnl_usdt,
                'pnl_rate': pnl_rate,
                'exit_type': exit_type
            }
        else:
            raise Exception(f"注文は執行されましたが、決済が完了しませんでした。ステータス: {order.get('status')}")

    except ccxt.ExchangeError as e:
        error_msg = f"取引所APIエラー: {e}"
        logging.error(f"❌ {symbol} 決済失敗: {error_msg}")
        return {'status': 'error', 'error_message': error_msg}
    except Exception as e:
        error_msg = f"予期せぬエラー: {e}"
        logging.error(f"❌ {symbol} 決済失敗: {error_msg}")
        return {'status': 'error', 'error_message': error_msg}


async def position_management_loop_async():
    """オープンポジションを監視し、SL/TPトリガーで決済を行う (10秒ごと実行)"""
    global OPEN_POSITIONS, EXCHANGE_CLIENT, IS_CLIENT_READY, ACCOUNT_EQUITY_USDT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return
        
    logging.info(f"⚙️ ポジション監視ループを開始します (管理中: {len(OPEN_POSITIONS)} 銘柄)。")

    # 最新のポジション情報を取得
    await fetch_open_positions() 
    
    # 最新の口座情報を取得 (清算価格が変更されていないか確認のため)
    await fetch_account_status()
    
    # 決済が必要なポジションを一時的に保持
    positions_to_close = []
    
    # リアルタイム価格を一括取得
    symbols_to_fetch = list(set([pos['symbol'] for pos in OPEN_POSITIONS]))
    if not symbols_to_fetch:
        return

    # 一括でティッカーを取得 (fetch_tickersは非同期メソッドなのでawait)
    try:
        tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols_to_fetch)
    except Exception as e:
        logging.error(f"❌ ポジション監視中の価格取得失敗: {e}")
        return
        
    for position in OPEN_POSITIONS:
        symbol = position['symbol']
        side = position['side']
        entry_price = position['entry_price']
        stop_loss = position['stop_loss']
        take_profit = position['take_profit']
        
        # 現在価格
        current_ticker = tickers.get(symbol)
        if not current_ticker:
            logging.warning(f"⚠️ {symbol} の最新価格を取得できませんでした。スキップします。")
            continue
            
        current_price = current_ticker.get('last')
        if not current_price:
            continue
            
        exit_type = None

        if side == 'long':
            # ロングの場合: SLトリガー (価格 <= SL) または TPトリガー (価格 >= TP)
            if stop_loss > 0 and current_price <= stop_loss:
                exit_type = 'Stop Loss'
            elif take_profit > 0 and current_price >= take_profit:
                exit_type = 'Take Profit'
        
        elif side == 'short':
            # ショートの場合: SLトリガー (価格 >= SL) または TPトリガー (価格 <= TP)
            if stop_loss > 0 and current_price >= stop_loss:
                exit_type = 'Stop Loss'
            elif take_profit > 0 and current_price <= take_profit:
                exit_type = 'Take Profit'

        # 清算価格の動的チェック (マージンコール防止)
        liquidation_price = position.get('liquidation_price', 0.0)
        
        if liquidation_price > 0.0:
            liquidation_margin = 0.005 # 清算価格の0.5%手前で強制決済
            
            if side == 'long' and current_price <= liquidation_price * (1 + liquidation_margin):
                exit_type = 'Liquidation Prevention'
            elif side == 'short' and current_price >= liquidation_price * (1 - liquidation_margin):
                exit_type = 'Liquidation Prevention'

        if exit_type:
            positions_to_close.append((position, exit_type, current_price))

    # 決済実行
    if positions_to_close:
        for position, exit_type, current_price in positions_to_close:
            
            trade_result = await close_position(position, exit_type, current_price)
            
            if trade_result and trade_result.get('status') == 'ok':
                # 決済成功ログ
                await send_telegram_notification(
                    format_telegram_message(position, "ポジション決済", get_current_threshold(GLOBAL_MACRO_CONTEXT), trade_result, exit_type)
                )
                log_signal(position, f"Trade Exit: {exit_type}", trade_result)
                
                # OPEN_POSITIONS から決済したポジションを削除
                OPEN_POSITIONS = [p for p in OPEN_POSITIONS if not (p['symbol'] == position['symbol'] and p['side'] == position['side'])]
                logging.info(f"✅ {position['symbol']} ({position['side'].upper()}) のポジションを管理リストから削除しました。")
            else:
                logging.error(f"❌ {position['symbol']} ({position['side'].upper()}) の決済に失敗しました。管理を継続します。")


async def main_bot_loop():
    """
    ボットの主要な分析・取引実行ロジック (1分ごと実行)
    """
    global LAST_SUCCESS_TIME, CURRENT_MONITOR_SYMBOLS, LAST_ANALYSIS_SIGNALS, GLOBAL_MACRO_CONTEXT, IS_FIRST_MAIN_LOOP_COMPLETED, LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, ACCOUNT_EQUITY_USDT
    
    start_time = time.time()
    
    if not IS_CLIENT_READY:
        logging.warning("⚠️ CCXTクライアントが準備できていません。初期化を待機します...")
        
        # クライアントが準備できていなければ、再度初期化を試みる
        if not await initialize_exchange_client():
            # 初期化に失敗した場合は、ループを終了し、次のスケジューラを待つ
            return 
    
    # 1. 口座ステータスの取得 (最初に実行)
    account_status = await fetch_account_status()
    if account_status.get('error'):
        logging.critical("❌ 口座情報取得失敗。取引を停止します。")
        return # 致命的なエラーのため取引停止
    
    # 2. マクロコンテキストの取得 (FGIなど)
    GLOBAL_MACRO_CONTEXT = await fetch_macro_context()
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    logging.info(f"📊 動的取引閾値: {current_threshold*100:.2f} / 100")
    
    # 3. 監視対象銘柄の更新 (定期的に実行)
    if time.time() - LAST_SUCCESS_TIME > 60 * 60 * 4 or not CURRENT_MONITOR_SYMBOLS or not IS_FIRST_MAIN_LOOP_COMPLETED: # 4時間ごと
        CURRENT_MONITOR_SYMBOLS = await fetch_top_symbols(TOP_SYMBOL_LIMIT)
        
    if not CURRENT_MONITOR_SYMBOLS:
        logging.error("❌ 監視対象銘柄リストが空です。")
        return
        
    # 4. 市場分析とシグナル生成
    all_signals = await analyze_market_data_async(CURRENT_MONITOR_SYMBOLS, GLOBAL_MACRO_CONTEXT)
    LAST_ANALYSIS_SIGNALS = all_signals
    
    # 5. シグナルのフィルタリングと取引実行
    
    # a. 閾値以上のシグナルにフィルタリング
    tradable_signals = [s for s in all_signals if s['score'] >= current_threshold and s['rr_ratio'] >= 1.0]
    
    # b. 最終的に取引するシグナルを決定 (最もスコアの高い1つ、かつクールダウン期間外)
    final_trade_signals = []
    
    for signal in tradable_signals:
        symbol_timeframe = f"{signal['symbol']}_{signal['timeframe']}"
        
        # 既に直近で取引した銘柄はスキップ
        last_trade_time = LAST_SIGNAL_TIME.get(symbol_timeframe, 0)
        if time.time() - last_trade_time < TRADE_SIGNAL_COOLDOWN:
            continue
            
        # 既にポジションを保有している銘柄はスキップ (両建てしないポリシー)
        is_already_in_position = any(p['symbol'] == signal['symbol'] for p in OPEN_POSITIONS)
        if is_already_in_position:
            logging.info(f"ℹ️ {signal['symbol']} は既にポジションを保有しているため取引をスキップします。")
            continue
            
        final_trade_signals.append(signal)
        
        if len(final_trade_signals) >= TOP_SIGNAL_COUNT:
            break

    # c. 取引の実行
    for signal in final_trade_signals:
        
        # 現在価格の最終チェック (約定価格の参照用)
        try:
            # ★ 修正適用: fetch_tickerはコルーチンなのでawaitする (約1112行目)
            ticker = await EXCHANGE_CLIENT.fetch_ticker(signal['symbol']) 
            current_price = ticker['last']
            signal['current_price'] = current_price # リアルタイム価格に更新
            
        except Exception as e:
            logging.error(f"❌ 最終価格取得エラー ({signal['symbol']}): {e}")
            await send_telegram_notification(f"🚨 **価格取得エラー**\n取引直前の価格取得に失敗しました: `{e}`")
            continue

        trade_result = await trade_execution_logic(signal)
        
        # 取引結果の通知とロギング
        if trade_result and trade_result.get('status') == 'ok':
            # 成功時: クールダウン時間を更新
            symbol_timeframe = f"{signal['symbol']}_{signal['timeframe']}"
            LAST_SIGNAL_TIME[symbol_timeframe] = time.time()
            
            # Telegram通知
            await send_telegram_notification(
                format_telegram_message(signal, "取引シグナル", current_threshold, trade_result)
            )
            # ローカルファイルログ
            log_signal(signal, "Trade Entry", trade_result)
            
        else:
            # 失敗時: Telegram通知は trade_execution_logic 内で発生しているはずだが、念のため。
            logging.error(f"❌ {signal['symbol']} の取引実行に失敗しました。")
            await send_telegram_notification(
                format_telegram_message(signal, "取引シグナル", current_threshold, trade_result or {'status': 'error', 'error_message': '取引実行ロジック内で失敗'})
            )
            log_signal(signal, "Trade Entry Failure", trade_result)
            
    # 6. 毎時/初回起動通知
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        # 初回起動完了通知
        await send_telegram_notification(
            format_startup_message(
                account_status, 
                GLOBAL_MACRO_CONTEXT, 
                len(CURRENT_MONITOR_SYMBOLS), 
                current_threshold, 
                "v20.0.17"
            )
        )
        IS_FIRST_MAIN_LOOP_COMPLETED = True
        
    # 7. WebShareのデータ更新 (取引履歴、ステータス、シグナル)
    if time.time() - LAST_WEBSHARE_UPLOAD_TIME > WEBSHARE_UPLOAD_INTERVAL:
        webshare_data = {
            'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            'account_equity': ACCOUNT_EQUITY_USDT,
            'open_positions': OPEN_POSITIONS,
            'last_signals': LAST_ANALYSIS_SIGNALS[:5], # トップ5シグナル
            'macro_context': GLOBAL_MACRO_CONTEXT
        }
        await send_webshare_update(webshare_data)
        LAST_WEBSHARE_UPLOAD_TIME = time.time()
        
    LAST_SUCCESS_TIME = time.time()
    end_time = time.time()
    logging.info(f"✅ メインループが正常に完了しました (実行時間: {end_time - start_time:.2f}秒)。")

# ====================================================================================
# SCHEDULERS & ENTRY POINT
# ====================================================================================

async def main_bot_scheduler():
    """メインループを定期実行するスケジューラ (1分ごと)"""
    # 起動時に一度CCXTクライアントの初期化を試みる
    await initialize_exchange_client() 
    
    while True:
        try:
            await main_bot_loop()
        except Exception as e:
            # 致命的なエラーの捕捉 (非同期関数内で発生し、awaitされていない場合もある)
            logging.critical(f"❌ メインループ実行中に致命的なエラー: {e}", exc_info=True)
            await send_telegram_notification(f"🚨 **致命的なエラー**\nメインループでエラーが発生しました: `{e}`")

        # 待機時間を LOOP_INTERVAL (60秒) に基づいて計算
        # 実行時間も考慮して、正確な間隔で実行されるように調整
        wait_time = max(1, LOOP_INTERVAL - (time.time() - LAST_SUCCESS_TIME))
        logging.info(f"次のメインループまで {wait_time:.1f} 秒待機します。")
        await asyncio.sleep(wait_time)


# TP/SL監視用スケジューラ
async def position_monitor_scheduler():
    """TP/SL監視ループを定期実行するスケジューラ (10秒ごと)"""
    while True:
        try:
            await position_management_loop_async()
        except Exception as e:
            logging.critical(f"❌ ポジション監視ループ実行中に致命的なエラー: {e}", exc_info=True)

        await asyncio.sleep(MONITOR_INTERVAL) # MONITOR_INTERVAL (10秒) ごとに実行

# FastAPIアプリケーション定義
app = FastAPI(title="Apex BOT API", version="v20.0.17")

@app.get("/health")
async def healthcheck():
    """ヘルスチェックエンドポイント (変更なし)"""
    global IS_CLIENT_READY
    
    status_code = 503
    
    if IS_CLIENT_READY:
        status_code = 200
        message = "Apex BOT Service is running and CCXT client is ready."
    else:
        # クライアント初期化中/未完了の場合、警告を返す（ただし200を維持してダウン判定は避ける）
        status_code = 200 
        message = "Apex BOT Service is running (Client initializing)."
        
    return JSONResponse(
        status_code=status_code,
        content={"status": message, "version": "v20.0.17", "timestamp": datetime.now(JST).isoformat()} # バージョン更新
    )


@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時に実行 (タスク起動の修正)"""
    logging.info("BOTサービスを開始しました。")
    
    # 💡 Patch 57の修正: 初期化タスクの即時実行を削除し、競合を避ける。
    
    # メインループのスケジューラをバックグラウンドで開始 (1分ごと)
    asyncio.create_task(main_bot_scheduler())
    
    # ポジション監視ループのスケジューラをバックグラウンドで開始 (10秒ごと)
    asyncio.create_task(position_monitor_scheduler())


# エラーハンドラ (変更なし)
@app.exception_handler(Exception)
async def default_exception_handler(request, exc):
    """捕捉されなかった例外を処理し、ログに記録する (変更なし)"""
    
    # CCXT RequestTimeoutの後に aiohttp の警告が出るのは一般的
    if "Unclosed" not in str(exc):
        logging.error(f"❌ Uncaught Exception: {type(exc).__name__} - {exc}")
    
    return JSONResponse(
        status_code=500,
        content={"message": "Internal Server Error", "error_type": type(exc).__name__}
    )
    
if __name__ == "__main__":
    # CCXTはバージョン番号を持たないため、ログに警告を追加
    if not hasattr(ccxt, 'version'):
        logging.warning("⚠️ CCXTのバージョン情報を取得できませんでした。インストールされているバージョンを確認してください。")
        
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
