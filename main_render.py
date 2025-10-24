# ====================================================================================
# Apex BOT v20.0.17 - Future Trading / 10x Leverage 
# (Patch 63: MEXC set_leverage FIX for Cross Margin)
#
# 改良・修正点:
# 1. 【バグ修正: Patch 63】MEXCでの set_leverage 呼び出し時に、クロスマージン ('openType': 2) を明示的に指定するように修正し、
#    set_leverage エラー ("mexc setLeverage() requires a positionId parameter or a symbol argument...") を解決。
# 2. 【ロジック維持: Patch 62】取引所シンボルID取得ロジックの修正を維持。
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
                    #   'positionType: 3' (Both sides - One-Way) はデフォルトのモードを維持するために入れているが、
                    #   openType=2 だけでもクロス設定が可能か確認。CCXTドキュメントに基づき openType=2 を追加。
                    
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
        logging.critical(f"❌ 口座ステータス取得失敗 (認証エラー): {e}")
    except Exception as e:
        # Patch 54でカバーしきれない予期せぬエラー 
        logging.error(f"❌ 口座ステータス取得失敗 (予期せぬエラー): {e}", exc_info=True)

    return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}

async def adjust_order_amount_by_base_units(symbol: str, target_base_amount: float) -> Optional[float]:
    """
    指定されたBase通貨建ての目標取引数量を、取引所の最小数量および数量精度に合わせて調整する。
    (変更なし)
    """
    global EXCHANGE_CLIENT
    
    # 💡 adjust_order_amount_by_base_units内での市場情報取得を修正 (Future/Swapを優先)
    market = None
    try:
        if EXCHANGE_CLIENT.id == 'mexc':
            for mkt in EXCHANGE_CLIENT.markets.values():
                if mkt['symbol'] == symbol and mkt['type'] in ['swap', 'future']:
                    market = mkt
                    break
        
        if not market:
             market = EXCHANGE_CLIENT.market(symbol) # Fallback to default
             
    except Exception:
         logging.error(f"❌ 注文数量調整エラー: {symbol} の市場情報が未ロードです。")
         return None
        
    if not market:
         logging.error(f"❌ 注文数量調整エラー: {symbol} の市場情報が見つかりません。")
         return None
         
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
        # 💡 fetch_ohlcvは 'defaultType': 'future' の設定で呼び出されるため、通常は問題ない
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
    """テクニカルインジケーターを計算する (変更なし)"""
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
        df['ATR'] = atr_data
    else:
        df['ATR'] = np.nan
    
    return df

def analyze_signals(df: pd.DataFrame, symbol: str, timeframe: str, macro_context: Dict) -> List[Dict]:
    """分析ロジックに基づき、取引シグナルを生成する (Long/Short両方に対応)"""

    if df.empty or df['SMA200'].isnull().all() or df['close'].isnull().iloc[-1] or df['ATR'].isnull().iloc[-1]: 
        return []
        
    current_price = df['close'].iloc[-1]
    current_atr = df['ATR'].iloc[-1] 
    all_signals: List[Dict] = []
    
    current_threshold = get_current_threshold(macro_context)
    
    # --- スコアリングの共通定数 ---
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    
    BASE_RRR = 1.5  
    MAX_SCORE_FOR_RRR = 0.85
    MAX_RRR = 3.0
    
    # --------------------------------------------------------------------------------
    # 1. ロングシグナル (Long Signal)
    # --------------------------------------------------------------------------------
    if df['SMA200'].iloc[-1] is not None and current_price > df['SMA200'].iloc[-1]:
        
        score = BASE_SCORE 
        
        # Long用 FGIボーナス (FGIがActive/Greedに傾くとプラス)
        sentiment_fgi_proxy_bonus = (fgi_proxy / FGI_ACTIVE_THRESHOLD) * FGI_PROXY_BONUS_MAX if abs(fgi_proxy) <= FGI_ACTIVE_THRESHOLD else (FGI_PROXY_BONUS_MAX if fgi_proxy > 0 else -FGI_PROXY_BONUS_MAX)
        
        tech_data = {
            'long_term_reversal_penalty_value': 0.0, # Longトレンド一致のためペナルティ回避
            'structural_pivot_bonus': STRUCTURAL_PIVOT_BONUS, 
            'macd_penalty_value': 0.0, 
            'obv_momentum_bonus_value': OBV_MOMENTUM_BONUS, 
            'liquidity_bonus_value': LIQUIDITY_BONUS_MAX, 
            'sentiment_fgi_proxy_bonus': sentiment_fgi_proxy_bonus, 
            'forex_bonus': 0.0,
            'volatility_penalty_value': 0.0,
        }
        
        # スコア計算
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
        
        
        # SL/TP calculation (Long)
        atr_sl_range = current_atr * ATR_MULTIPLIER_SL
        stop_loss = current_price - atr_sl_range
        min_sl_range = current_price * MIN_RISK_PERCENT
        if atr_sl_range < min_sl_range:
             stop_loss = current_price - min_sl_range
             atr_sl_range = min_sl_range
        
        risk_usdt_per_unit = current_price - stop_loss 
        
        # RRR calculation
        if score > current_threshold:
            # Scoreが閾値を超えた場合にRRRを動的に上げる
            score_ratio = min(1.0, (score - current_threshold) / (MAX_SCORE_FOR_RRR - current_threshold))
            dynamic_rr_ratio = BASE_RRR + (MAX_RRR - BASE_RRR) * score_ratio
        else:
            dynamic_rr_ratio = BASE_RRR 
            
        take_profit = current_price + (risk_usdt_per_unit * dynamic_rr_ratio)
        
        rr_ratio = dynamic_rr_ratio 
        liquidation_price = calculate_liquidation_price(current_price, LEVERAGE, side='long', maintenance_margin_rate=MIN_MAINTENANCE_MARGIN_RATE)
        
        # Risk-based sizing
        max_risk_usdt = ACCOUNT_EQUITY_USDT * MAX_RISK_PER_TRADE_PERCENT
        target_base_amount_units = max_risk_usdt / risk_usdt_per_unit 
        notional_value = target_base_amount_units * current_price 
            
        if score > current_threshold and rr_ratio >= 1.0 and risk_usdt_per_unit > 0:
            all_signals.append({
                'symbol': symbol,
                'timeframe': timeframe,
                'action': 'buy', 
                'side': 'long',  
                'score': score,
                'rr_ratio': rr_ratio, 
                'entry_price': current_price,
                'stop_loss': stop_loss, 
                'take_profit': take_profit, 
                'liquidation_price': liquidation_price, 
                'risk_usdt_per_unit': risk_usdt_per_unit, 
                'risk_usdt': max_risk_usdt, 
                'notional_value': notional_value, 
                'lot_size_units': target_base_amount_units, 
                'tech_data': tech_data, 
            })


    # --------------------------------------------------------------------------------
    # 2. ショートシグナル (Short Signal) - ロジックを反転
    # --------------------------------------------------------------------------------
    if df['SMA200'].iloc[-1] is not None and current_price < df['SMA200'].iloc[-1]:
        
        score = BASE_SCORE 
        
        # Short用 FGIボーナス (FGIがSlump/Fearに傾くとプラス)
        # FGIの影響を反転
        sentiment_fgi_proxy_bonus_short = (fgi_proxy / FGI_ACTIVE_THRESHOLD) * FGI_PROXY_BONUS_MAX if abs(fgi_proxy) <= FGI_ACTIVE_THRESHOLD else (FGI_PROXY_BONUS_MAX if fgi_proxy < 0 else -FGI_PROXY_BONUS_MAX) 
        
        # Longのペナルティ項目をShortのボーナスとして解釈
        long_term_trend_bonus_short = LONG_TERM_REVERSAL_PENALTY 
        macd_momentum_bonus_short = MACD_CROSS_PENALTY
        
        tech_data_short = {
            'long_term_reversal_penalty_value': long_term_trend_bonus_short, # Longのペナルティ回避がShortのボーナスに
            'structural_pivot_bonus': STRUCTURAL_PIVOT_BONUS, # Longと同じ
            'macd_penalty_value': macd_momentum_bonus_short, # Longのペナルティ回避がShortのボーナスに
            'obv_momentum_bonus_value': OBV_MOMENTUM_BONUS, # Longと同じ
            'liquidity_bonus_value': LIQUIDITY_BONUS_MAX, # Longと同じ
            'sentiment_fgi_proxy_bonus': sentiment_fgi_proxy_bonus_short, # ショート向けFGIボーナス
            'forex_bonus': 0.0,
            'volatility_penalty_value': 0.0, # Longと同じ
        }
        
        # スコア計算 (Short方向の条件を考慮して係数を適用)
        score += (
            tech_data_short['long_term_reversal_penalty_value'] + # 長期トレンド一致ボーナス (SMA200下)
            tech_data_short['structural_pivot_bonus'] + 
            tech_data_short['macd_penalty_value'] + # モメンタムボーナス
            tech_data_short['obv_momentum_bonus_value'] + 
            tech_data_short['liquidity_bonus_value'] + 
            tech_data_short['sentiment_fgi_proxy_bonus'] + 
            tech_data_short['forex_bonus'] +
            tech_data_short['volatility_penalty_value']
        )
        
        # SL/TP calculation (Short)
        
        # ATRによるSL幅 (絶対値)
        atr_sl_range = current_atr * ATR_MULTIPLIER_SL
        
        # SL価格の計算: Shortはエントリー価格より上
        stop_loss = current_price + atr_sl_range
        
        # SLリスク幅が価格の最小パーセンテージ (MIN_RISK_PERCENT) を下回っていないかチェック
        min_sl_range = current_price * MIN_RISK_PERCENT
        if atr_sl_range < min_sl_range:
             stop_loss = current_price + min_sl_range
             atr_sl_range = min_sl_range 
        
        # リスク額のUSD建て表現 (1単位あたりのUSDリスク)
        risk_usdt_per_unit = stop_loss - current_price # Shortは SL - Entry Price
        
        # RRR calculation (Longと同じロジック)
        if score > current_threshold:
            score_ratio = min(1.0, (score - current_threshold) / (MAX_SCORE_FOR_RRR - current_threshold))
            dynamic_rr_ratio = BASE_RRR + (MAX_RRR - BASE_RRR) * score_ratio
        else:
            dynamic_rr_ratio = BASE_RRR 
            
        # TP価格の計算: Shortはエントリー価格より下
        take_profit = current_price - (risk_usdt_per_unit * dynamic_rr_ratio)
        
        rr_ratio = dynamic_rr_ratio 
        liquidation_price = calculate_liquidation_price(current_price, LEVERAGE, side='short', maintenance_margin_rate=MIN_MAINTENANCE_MARGIN_RATE)

        # Risk-based sizing (Longと同じ計算)
        max_risk_usdt = ACCOUNT_EQUITY_USDT * MAX_RISK_PER_TRADE_PERCENT
        target_base_amount_units = max_risk_usdt / risk_usdt_per_unit 
        notional_value = target_base_amount_units * current_price 
            
        if score > current_threshold and rr_ratio >= 1.0 and risk_usdt_per_unit > 0:
            all_signals.append({
                'symbol': symbol,
                'timeframe': timeframe,
                'action': 'sell', # 'sell' for Short order
                'side': 'short', # Position side
                'score': score,
                'rr_ratio': rr_ratio, 
                'entry_price': current_price,
                'stop_loss': stop_loss, 
                'take_profit': take_profit, 
                'liquidation_price': liquidation_price, 
                'risk_usdt_per_unit': risk_usdt_per_unit, 
                'risk_usdt': max_risk_usdt, 
                'notional_value': notional_value, 
                'lot_size_units': target_base_amount_units, 
                'tech_data': tech_data_short, 
            })

    return all_signals

async def liquidate_position(position: Dict, exit_type: str, current_price: float) -> Optional[Dict]:
    """
    ポジションを決済する (成行売買を想定)。
    """
    global EXCHANGE_CLIENT
    symbol = position['symbol']
    amount = position['amount']
    entry_price = position['entry_price']
    side = position.get('side', 'long') # ポジションのサイドを取得
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 決済失敗: CCXTクライアントが準備できていません。")
        return {'status': 'error', 'error_message': 'CCXTクライアント未準備'}

    # 注文サイドとポジションサイドの決定
    if side == 'long':
        order_side = 'sell'
        position_side = 'LONG'
        log_message = f"{symbol} Close Long Market"
    else: # side == 'short'
        order_side = 'buy'
        position_side = 'SHORT'
        log_message = f"{symbol} Close Short Market"
    
    # 💡 【v20.0.16 FIX】取引所のシンボルIDを確実に先物/スワップ市場から取得
    exchange_symbol_id = symbol
    market = None
    try:
        if EXCHANGE_CLIENT.id == 'mexc':
            # MEXC specific: search for 'swap' or 'future' type
            for mkt in EXCHANGE_CLIENT.markets.values():
                if mkt['symbol'] == symbol and mkt['type'] in ['swap', 'future']:
                    market = mkt
                    break
            
            if market:
                exchange_symbol_id = market['id']
                logging.info(f"ℹ️ {symbol} の取引所シンボルIDを取得 (FIXED, 決済): {exchange_symbol_id} (市場タイプ: {market.get('type')})")
            else:
                 # Fallback to the default CCXT lookup, but log a warning
                 market = EXCHANGE_CLIENT.market(symbol)
                 exchange_symbol_id = market['id']
                 logging.warning(f"⚠️ {symbol} の先物/スワップ市場IDが見つかりませんでした。デフォルト ({market.get('type')}) のIDを使用 (決済): {exchange_symbol_id}")

        else:
            # For other exchanges, rely on the default CCXT lookup with 'defaultType: future'
            market = EXCHANGE_CLIENT.market(symbol)
            exchange_symbol_id = market['id']
            logging.info(f"ℹ️ {symbol} の取引所シンボルIDを取得 (Default, 決済): {exchange_symbol_id} (市場タイプ: {market.get('type')})")
        
    except Exception as e:
        logging.error(f"❌ 取引所シンボルID取得失敗: {symbol}. {e}")
        exchange_symbol_id = symbol # 失敗した場合、フォールバックとして元のシンボルを使用
        
    # 1. 注文実行（先物ポジションの決済: 反対売買の成行注文）
    try:
        # closePosition: True はポジションを全て決済するためのCCXT共通パラメータ
        # MEXCでは positionSide をパラメーターとして渡す必要がある
        params = {'positionSide': position_side, 'closePosition': True} 
        
        if TEST_MODE:
            logging.info(f"✨ TEST MODE: {log_message} ({exit_type}). 数量: {amount:.8f} の注文をシミュレート。")
            order = {
                'id': f"test-exit-{uuid.uuid4()}",
                'symbol': symbol,
                'side': order_side,
                'amount': amount,
                'price': current_price, 
                'status': 'closed', 
                'datetime': datetime.now(timezone.utc).isoformat(),
                'filled': amount,
                'cost': amount * current_price, 
            }
        else:
            order = await EXCHANGE_CLIENT.create_order(
                symbol=exchange_symbol_id, # ★ シンボルIDを使用
                type='market',
                side=order_side, # 'buy' (Short決済) or 'sell' (Long決済)
                amount=amount, 
                params=params
            )
            logging.info(f"✅ 決済実行成功: {log_message}. 数量: {amount:.8f} ({exit_type})")

        filled_amount_val = order.get('filled', 0.0)
        cost_val = order.get('cost', 0.0) 

        if filled_amount_val <= 0:
            return {'status': 'error', 'error_message': '約定数量ゼロ'}

        exit_price = cost_val / filled_amount_val if filled_amount_val > 0 else current_price

        # PnL計算 (概算) - ロングとショートで反転
        initial_notional_cost = position['filled_usdt']
        final_notional_value = cost_val 
        
        if side == 'long':
            # Long: 決済金額 - エントリーコスト
            pnl_usdt = final_notional_value - initial_notional_cost 
        else:
            # Short: エントリーコスト - 決済金額 (Shortは価格が下がると利益なので)
            pnl_usdt = initial_notional_cost - final_notional_value
            
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
    """オープンポジションを監視し、SL/TPをチェックして決済する"""
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
        entry_price = position['entry_price']
        side = position.get('side', 'long') # ポジションのサイドを取得

        try:
            # 1. 最新の現在価格を取得
            ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
            current_price = ticker['last']
            
            trade_signal_data = {
                'symbol': symbol,
                'timeframe': position['timeframe'],
                'side': side, # ショート対応
                'score': current_threshold, 
                'rr_ratio': position['rr_ratio'],
                'entry_price': entry_price,
                'stop_loss': sl,
                'take_profit': tp,
                'liquidation_price': position['liquidation_price'],
            }
            
            # 2. SLチェック (LongとShortで判定ロジックを反転)
            sl_triggered = False
            if side == 'long' and current_price <= sl:
                sl_triggered = True
                logging.warning(f"🚨 SLトリガー (Long): {symbol} - 現在価格 {format_price(current_price)} <= SL {format_price(sl)}")
            elif side == 'short' and current_price >= sl:
                sl_triggered = True
                logging.warning(f"🚨 SLトリガー (Short): {symbol} - 現在価格 {format_price(current_price)} >= SL {format_price(sl)}")
                
            if sl_triggered:
                trade_result = await liquidate_position(position, 'SL', current_price)
                closed_positions.append({'pos': position, 'result': trade_result, 'signal_data': trade_signal_data, 'exit_type': 'SL'})
                continue

            # 3. TPチェック (LongとShortで判定ロジックを反転)
            tp_triggered = False
            if side == 'long' and current_price >= tp:
                tp_triggered = True
                logging.info(f"🎉 TPトリガー (Long): {symbol} - 現在価格 {format_price(current_price)} >= TP {format_price(tp)}")
            elif side == 'short' and current_price <= tp:
                tp_triggered = True
                logging.info(f"🎉 TPトリガー (Short): {symbol} - 現在価格 {format_price(current_price)} <= TP {format_price(tp)}")
                
            if tp_triggered:
                trade_result = await liquidate_position(position, 'TP', current_price)
                closed_positions.append({'pos': position, 'result': trade_result, 'signal_data': trade_signal_data, 'exit_type': 'TP'})
                continue
                
            # 4. 清算価格チェック (予防的なロギング)
            liq_price = position['liquidation_price']
            
            if side == 'long' and current_price < liq_price * 1.05: # ロング: 清算価格の5%以内に迫っている場合
                logging.critical(f"⚠️ 清算価格接近アラート (Long): {symbol}. 現在価格 {format_price(current_price)} vs 清算価格 {format_price(liq_price)}")
            elif side == 'short' and current_price > liq_price * 0.95: # ショート: 清算価格の5%以内に迫っている場合
                 logging.critical(f"⚠️ 清算価格接近アラート (Short): {symbol}. 現在価格 {format_price(current_price)} vs 清算価格 {format_price(liq_price)}")

        except ccxt.NetworkError as e:
            logging.error(f"❌ 監視ループ: {symbol} - ネットワークエラー: {e}")
        except Exception as e:
            logging.critical(f"❌ 監視ループ: {symbol} - 予期せぬエラー: {e}", exc_info=True)


    # 6. ポジションの更新
    if closed_positions:
        
        # 決済通知とログ記録
        for entry in closed_positions:
            pos = entry['pos']
            result = entry['result']
            signal_data = entry['signal_data']
            exit_type = entry['exit_type']
            
            # ログ記録
            log_signal(signal_data, f'TRADE_EXIT_{exit_type}', trade_result=result)
            
            # Telegram通知
            if result and result['status'] == 'ok':
                 # 決済通知の送信 (スコアは使用しない)
                 await send_telegram_notification(format_telegram_message(
                     signal=signal_data, 
                     context="ポジション決済", 
                     current_threshold=current_threshold, 
                     trade_result=result,
                     exit_type=exit_type
                 ))
            else:
                 logging.error(f"❌ 決済ログ/通知失敗: {pos['symbol']} ({exit_type}). 結果: {result}")
                 await send_telegram_notification(f"🚨 **決済失敗アラート**\\n{pos['symbol']} の {exit_type} 決済が失敗しました。ログを確認してください。")

        # グローバルな OPEN_POSITIONS から決済されたものを削除
        OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p['symbol'] not in {cp['pos']['symbol'] for cp in closed_positions}]
        logging.info(f"✅ ポジション監視完了。{len(closed_positions)} 件のポジションを決済し、残り {len(OPEN_POSITIONS)} 件。")


async def execute_trade(signal: Dict) -> Optional[Dict]:
    """
    CCXTを使用して取引を執行し、ポジション情報をグローバル変数に格納する。
    Long/Shortに対応。
    """
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    symbol = signal['symbol']
    # 調整後のBase通貨建ての数量
    target_amount = signal['lot_size_units'] 
    action = signal['action'] # 'buy' for long, 'sell' for short
    side = signal['side'] # 'long' or 'short'
    
    if TEST_MODE:
        # テストモードではシミュレーション結果を返す
        filled_amount = target_amount
        filled_usdt = signal['notional_value']
        
        # ポジションリストに追加 (SL/TP監視用)
        OPEN_POSITIONS.append({
            'symbol': symbol,
            'amount': filled_amount, 
            'entry_price': signal['entry_price'],
            'stop_loss': signal['stop_loss'],
            'take_profit': signal['take_profit'],
            'liquidation_price': signal['liquidation_price'],
            'filled_usdt': filled_usdt,
            'rr_ratio': signal['rr_ratio'],
            'timeframe': signal['timeframe'],
            'side': side, # ★ ポジションのサイドを保存
        })
        logging.info(f"✨ TEST MODE: {symbol} - {side.upper()}取引をシミュレート。ロット: {filled_amount:.4f}")
        return {
            'status': 'ok',
            'filled_amount': filled_amount,
            'filled_usdt': filled_usdt,
            'entry_price': signal['entry_price'],
            'error_message': None
        }

    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 取引執行失敗: CCXTクライアントが準備できていません。")
        return {'status': 'error', 'error_message': 'CCXTクライアント未準備'}

    # 1. 数量の最終調整 (取引所の精度と最小ロットに合わせる)
    final_amount = await adjust_order_amount_by_base_units(symbol, target_amount)
    if final_amount is None:
        return {'status': 'error', 'error_message': 'ロットサイズの調整に失敗'}
        
    current_price = signal['entry_price']
    
    # 💡 【v20.0.16 FIX】取引所のシンボルIDを確実に先物/スワップ市場から取得
    exchange_symbol_id = symbol
    market = None
    try:
        if EXCHANGE_CLIENT.id == 'mexc':
            # MEXC specific: search for 'swap' or 'future' type
            for mkt in EXCHANGE_CLIENT.markets.values():
                if mkt['symbol'] == symbol and mkt['type'] in ['swap', 'future']:
                    market = mkt
                    break
            
            if market:
                exchange_symbol_id = market['id']
                logging.info(f"ℹ️ {symbol} の取引所シンボルIDを取得 (FIXED): {exchange_symbol_id} (市場タイプ: {market.get('type')})")
            else:
                 # Fallback to the default CCXT lookup, but log a warning
                 market = EXCHANGE_CLIENT.market(symbol)
                 exchange_symbol_id = market['id']
                 logging.warning(f"⚠️ {symbol} の先物/スワップ市場IDが見つかりませんでした。デフォルト ({market.get('type')}) のIDを使用: {exchange_symbol_id}")

        else:
            # For other exchanges, rely on the default CCXT lookup with 'defaultType: future'
            market = EXCHANGE_CLIENT.market(symbol)
            exchange_symbol_id = market['id']
            logging.info(f"ℹ️ {symbol} の取引所シンボルIDを取得 (Default): {exchange_symbol_id} (市場タイプ: {market.get('type')})")
        
    except Exception as e:
        logging.error(f"❌ 取引所シンボルID取得失敗: {symbol}. {e}")
        exchange_symbol_id = symbol # 失敗した場合、フォールバックとして元のシンボルを使用
    
    # 2. 注文実行（成行注文）
    try:
        # 注文のサイドとポジションサイドを決定
        order_side = 'buy' if side == 'long' else 'sell'
        position_side = 'LONG' if side == 'long' else 'SHORT'
        
        # 先物注文パラメータ: MEXCでは positionSide をパラメーターとして渡す必要がある
        params = {'positionSide': position_side} 
        
        order = await EXCHANGE_CLIENT.create_order(
            symbol=exchange_symbol_id, # ★ シンボルIDを使用
            type='market',
            side=order_side, # 'buy' or 'sell'
            amount=final_amount, 
            params=params
        )
        logging.info(f"✅ 取引執行成功: {symbol} - {side.upper()}成行注文を送信。ロット: {final_amount:.4f}")

        # 3. 約定結果の確認 (ここでは簡易的に処理)
        filled_amount = order.get('filled', final_amount)
        cost = order.get('cost', filled_amount * current_price) # 名目価値 (約定金額)

        if filled_amount <= 0:
             return {'status': 'error', 'error_message': '約定数量ゼロ'}
             
        # 約定平均価格 (約定が分散する場合もあるため、cost/filled_amountが正確だが、ここでは簡易的にシグナルのentry_priceを使用)
        actual_entry_price = current_price 
        
        # 4. ポジションリストに追加 (SL/TP監視用)
        OPEN_POSITIONS.append({
            'symbol': symbol,
            'amount': filled_amount, 
            'entry_price': actual_entry_price,
            'stop_loss': signal['stop_loss'],
            'take_profit': signal['take_profit'],
            'liquidation_price': signal['liquidation_price'],
            'filled_usdt': cost, 
            'rr_ratio': signal['rr_ratio'],
            'timeframe': signal['timeframe'],
            'side': side, # ★ ポジションのサイドを保存
        })

        return {
            'status': 'ok',
            'filled_amount': filled_amount,
            'filled_usdt': cost,
            'entry_price': actual_entry_price,
            'error_message': None
        }

    except ccxt.InsufficientFunds as e:
        logging.error(f"❌ 取引執行失敗 (資金不足): {symbol}. {e}")
        return {'status': 'error', 'error_message': f'資金不足: {e}'}
    except ccxt.ExchangeError as e:
        logging.error(f"❌ 取引執行失敗 (取引所エラー): {symbol}. {e}")
        return {'status': 'error', 'error_message': f'取引所エラー: {e}'}
    except Exception as e:
        logging.error(f"❌ 取引執行失敗 (予期せぬエラー): {symbol}. {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'予期せぬエラー: {e}'}


async def main_bot_loop():
    """BOTのメイン処理: データ取得、分析、取引執行を制御する"""
    global EXCHANGE_CLIENT, LAST_SUCCESS_TIME, LAST_SIGNAL_TIME, LAST_ANALYSIS_SIGNALS, IS_FIRST_MAIN_LOOP_COMPLETED, GLOBAL_MACRO_CONTEXT
    global LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, LAST_WEBSHARE_UPLOAD_TIME
    
    if not IS_CLIENT_READY:
        logging.warning("⚠️ メインループスキップ: CCXTクライアントが初期化されていません。再試行します...")
        await initialize_exchange_client() # クライアント初期化を再試行
        return
        
    start_time = time.time()
    
    # 1. 口座ステータスの更新
    account_status = await fetch_account_status()
    if account_status.get('error'):
        # エラーが解決したか確認
        if ACCOUNT_EQUITY_USDT > 0:
             # 一時的なエラーで、以前取得した残高が残っている場合
             logging.warning("⚠️ 口座ステータスの取得に一時的に失敗しましたが、前回の残高を使用して続行します。")
             account_status['total_usdt_balance'] = ACCOUNT_EQUITY_USDT
             account_status['error'] = False
        else:
             logging.error("❌ メインループ失敗: 口座ステータスの取得に失敗し、残高がゼロです。取引をスキップします。")
             return 
        
    # 2. マクロ環境データの更新
    macro_data = await fetch_fgi_data()
    GLOBAL_MACRO_CONTEXT.update(macro_data)
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)

    # 3. 監視銘柄の市場データ取得と分析
    tasks = []
    
    # 最新の監視対象銘柄を取得 (デフォルトリストを使用)
    monitor_symbols = CURRENT_MONITOR_SYMBOLS 
    
    for symbol in monitor_symbols:
        for timeframe in TARGET_TIMEFRAMES:
            limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 1000)
            tasks.append(fetch_ohlcv_safe(symbol, timeframe, limit))
            
    ohlcv_results = await asyncio.gather(*tasks)
    
    all_signals: List[Dict] = []

    # 4. 分析実行
    data_index = 0
    for symbol in monitor_symbols:
        for timeframe in TARGET_TIMEFRAMES:
            df = ohlcv_results[data_index]
            data_index += 1
            
            if df is None or df.empty:
                continue
            
            # インジケーター計算
            df = calculate_indicators(df)

            # シグナル分析 (Long/Shortの両方の可能性を含むリストを返す)
            signals = analyze_signals(df, symbol, timeframe, GLOBAL_MACRO_CONTEXT)
            
            if signals:
                all_signals.extend(signals)

    # 5. シグナル処理と取引執行
    
    # 冷却期間チェック (12時間以内は同一銘柄で取引しない)
    cooldown_filtered_signals = []
    for signal in all_signals:
         symbol = signal['symbol']
         if time.time() - LAST_SIGNAL_TIME.get(symbol, 0) < TRADE_SIGNAL_COOLDOWN:
             logging.info(f"🕒 {symbol} は冷却期間 ({TRADE_SIGNAL_COOLDOWN/3600:.0f}時間) のためスキップします。") # ログを追加
             continue
         cooldown_filtered_signals.append(signal)

    # スコアの高い順にソート
    sorted_signals = sorted(cooldown_filtered_signals, key=lambda x: x['score'], reverse=True)
        
    # 現在オープン中のポジションのシンボルリスト
    open_symbols = {p['symbol'] for p in OPEN_POSITIONS}

    # フィルタリングされたシグナルからTop N個（ここではTop 1）を選択
    trade_signals = []
    for signal in sorted_signals:
        symbol = signal['symbol']
        
        # ポジション重複チェック (既にオープンしている銘柄はスキップ)
        if symbol in open_symbols:
            continue
            
        # スコアが閾値を超えているか最終チェック
        if signal['score'] < current_threshold:
             continue # 閾値未満のシグナルは無視
             
        trade_signals.append(signal)
        # Patch 59の修正: 常にTop 1のみを採用
        if len(trade_signals) >= TOP_SIGNAL_COUNT:
             break
        
    # 分析結果を保存
    LAST_ANALYSIS_SIGNALS = sorted_signals 
    
    # 取引実行
    if trade_signals:
        
        # Top 1シグナル
        signal_to_execute = trade_signals[0]
        symbol = signal_to_execute['symbol']
        
        logging.info(f"💡 最もスコアの高いシグナルを検出: {symbol} (Score: {signal_to_execute['score']:.2f}) - Top 1取引実行") # ログを修正
        
        # 取引執行
        trade_result = await execute_trade(signal_to_execute)
        
        # シグナルログと通知
        log_signal(signal_to_execute, 'TRADE_SIGNAL', trade_result=trade_result)
        await send_telegram_notification(format_telegram_message(
            signal=signal_to_execute, 
            context="取引シグナル", 
            current_threshold=current_threshold, 
            trade_result=trade_result
        ))
        
        # 成功した場合のみ、冷却期間をリセット
        if trade_result and trade_result.get('status') == 'ok':
            LAST_SIGNAL_TIME[symbol] = time.time()
            # 既にポジションを取得したため、これ以上は取引しない

    else:
        logging.info("📝 適切な取引シグナルは検出されませんでした。")

    
    # 6. 初回起動完了通知と定期通知 (1時間ごと)
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        # 初回起動完了通知
        await send_telegram_notification(format_startup_message(
            account_status, 
            GLOBAL_MACRO_CONTEXT, 
            len(monitor_symbols),
            current_threshold,
            "v20.0.17 (Patch 63)" # BOTバージョンを更新
        ))
        IS_FIRST_MAIN_LOOP_COMPLETED = True
        
    elif time.time() - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME > ANALYSIS_ONLY_INTERVAL:
        # 1時間ごとの分析専用通知
        # (ロジックが複雑になるため、ここでは省略し、初回起動通知に集約する)
        LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = time.time()

    # 7. WebShareログの送信
    if time.time() - LAST_WEBSHARE_UPLOAD_TIME > WEBSHARE_UPLOAD_INTERVAL:
        webshare_data = {
            'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            'account_equity': ACCOUNT_EQUITY_USDT,
            'open_positions_count': len(OPEN_POSITIONS),
            'top_signals': [_to_json_compatible(s) for s in LAST_ANALYSIS_SIGNALS[:3]],
            'macro_context': GLOBAL_MACRO_CONTEXT,
        }
        await send_webshare_update(webshare_data)
        LAST_WEBSHARE_UPLOAD_TIME = time.time()

    
    LAST_SUCCESS_TIME = time.time()
    loop_duration = time.time() - start_time
    logging.info(f"--- ✅ {datetime.now(JST).strftime('%Y/%m/%d %H:%M:%S')} - BOT LOOP END. 処理時間: {loop_duration:.2f} 秒 ---")


# メインループを定期実行するスケジューラ
async def main_bot_scheduler():
    """メインループを定期実行するスケジューラ (1分ごと)"""
    # 初回起動時には、すぐに実行せず、クライアントの準備を待つ
    await asyncio.sleep(5) 

    while True:
        try:
            # クライアントが未初期化の場合、初期化を試行
            if not IS_CLIENT_READY:
                 logging.info("クライアント未準備のため、初期化を試行します。")
                 # initialize_exchange_client() はここではブロッキングコールとして実行
                 await initialize_exchange_client() 
                 # 初期化後に再度待機
                 await asyncio.sleep(5) 
                 continue 
                 
            # メインループの実行
            await main_bot_loop()
            
        except Exception as e:
            logging.critical(f"❌ メインループ実行中に致命的なエラー: {e}", exc_info=True)
            await send_telegram_notification(f"🚨 **致命的なエラー**\\nメインループでエラーが発生しました: <code>{e}</code>")

        # 待機時間を LOOP_INTERVAL (60秒) に基づいて計算
        # 処理時間が長引いた場合は、その分待機時間を短縮する
        wait_time = max(1, LOOP_INTERVAL - (time.time() - LAST_SUCCESS_TIME))
        logging.info(f"次のメインループまで {wait_time:.1f} 秒待機します。")
        await asyncio.sleep(wait_time)

# TP/SL監視用スケジューラ (変更なし)
async def position_monitor_scheduler():
    """TP/SL監視ループを定期実行するスケジューラ (10秒ごと)"""
    while True:
        try:
            # クライアントが準備できていない場合はスキップ
            if IS_CLIENT_READY and OPEN_POSITIONS:
                 await position_management_loop_async()
                 
        except Exception as e:
            logging.critical(f"❌ ポジション監視ループ実行中に致命的なエラー: {e}", exc_info=True)

        await asyncio.sleep(MONITOR_INTERVAL) # MONITOR_INTERVAL (10秒) ごとに実行


# ====================================================================================
# FASTAPI & UVICORN SETUP
# ====================================================================================

# Uvicornの起動時に利用するインスタンス
app = FastAPI()

# 💡 UptimeRobot/ヘルスチェック対応のエンドポイント
@app.get("/")
async def health_check():
    """
    UptimeRobotなどの監視サービスがBOTの稼働状態を確認するためのエンドポイント。
    """
    if IS_CLIENT_READY:
        # クライアントが初期化済みであれば、より確実に稼働中と判断
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
        logging.error(f"❌ 予期せぬエラーが発生しました: {exc}", exc_info=True)
    
    return JSONResponse(
        status_code=500,
        content={"message": "Internal Server Error", "detail": str(exc)},
    )


# Uvicornのエントリーポイント (main_render.pyとして実行される場合)
if __name__ == "__main__":
    # Uvicorn実行は通常、外部の環境 (render.comなど) で行われるため、ここでは開発用起動をコメントアウト
    # uvicorn.run("main_render:app", host="0.0.0.0", port=10000, reload=True) 
    # 実行は 'uvicorn main_render:app --host 0.0.0.0 --port $PORT' コマンドで行う
    pass
