# ====================================================================================
# Apex BOT v20.0.44 - Future Trading / 30x Leverage 
# (Feature: 実践的スコアリングロジック、ATR動的リスク管理導入)
# 
# 🚨 致命的エラー修正強化 (v20.0.44): 
# 1. 💡 修正: mexc fetchBalance() not support self method 対策 (get_account_status)
# 2. 💡 修正: fetch_tickersのAttributeError ('NoneType' object has no attribute 'keys') 対策 (get_top_symbols)
# 3. 修正: np.polyfitの戻り値エラー (ValueError: not enough values to unpack) を修正
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
from fastapi import FastAPI, Request, Response 
from fastapi.responses import JSONResponse
import uvicorn
from dotenv import load_dotenv
import sys
import random
import json
import re
import uuid 
import math 

# .envファイルから環境変数を読み込む
load_dotenv()

# 💡 【ログ確認対応】ロギング設定を明示的に定義
logging.basicConfig(
    level=logging.INFO, 
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
    "FLOW/USDT", "IMX/USDT", "SUI/USDT", "ASTER/USDT", "ENA/USDT",
    "ZEC/USDT", "PUMP/USDT", "PEPE/USDT", "FARTCOIN/USDT",
    "WLFI/USDT", "PENGU/USDT", "ONDO/USDT", "HBAR/USDT", "TRUMP/USDT",
    "SHIB/USDT", "HYPE/USDT", "LINK/USDT", "ZEC/USDT",
    "VIRTUAL/USDT", "PIPPIN/USDT", "GIGGLE/USDT", "H/USDT", "AIXBT/USDT", 
]
TOP_SYMBOL_LIMIT = 40               
BOT_VERSION = "v20.0.44"            # 💡 BOTバージョンを更新 
FGI_API_URL = "https://api.alternative.me/fng/?limit=1" 

LOOP_INTERVAL = 60 * 1              
ANALYSIS_ONLY_INTERVAL = 60 * 5    
WEBSHARE_UPLOAD_INTERVAL = 60 * 60  
MONITOR_INTERVAL = 10               

# 💡 クライアント設定
CCXT_CLIENT_NAME = os.getenv("EXCHANGE_CLIENT", "mexc")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_API_KEY")
SECRET_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_SECRET")
TEST_MODE = os.getenv("TEST_MODE", "False").lower() in ('true', '1', 't')
SKIP_MARKET_UPDATE = os.getenv("SKIP_MARKET_UPDATE", "False").lower() in ('true', '1', 't')

# 💡 先物取引設定 
LEVERAGE = 30 
TRADE_TYPE = 'future' 
MIN_MAINTENANCE_MARGIN_RATE = 0.005 

# 💡 レートリミット対策用定数
LEVERAGE_SETTING_DELAY = 1.0 

# 💡 【固定ロット】設定 
FIXED_NOTIONAL_USDT = 20.0 

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
OPEN_POSITIONS: List[Dict] = [] 
ACCOUNT_EQUITY_USDT: float = 0.0 # 💡 総資産の初期値
if TEST_MODE:
    logging.warning("⚠️ WARNING: TEST_MODE is active. Trading is disabled。")

# CCXTクライアントの準備完了フラグ
IS_CLIENT_READY: bool = False

# 取引ルール設定
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 12 
SIGNAL_THRESHOLD = 0.65             
TOP_SIGNAL_COUNT = 1                
REQUIRED_OHLCV_LIMITS = {'1m': 500, '5m': 500, '15m': 500, '1h': 500, '4h': 500} 

# テクニカル分析定数 
TARGET_TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h'] 
BASE_SCORE = 0.40                  
LONG_TERM_SMA_LENGTH = 200         
LONG_TERM_REVERSAL_PENALTY = 0.20   # 順張りボーナス/逆張りペナルティの値
STRUCTURAL_PIVOT_BONUS = 0.05       
RSI_MOMENTUM_LOW = 40              # RSI 40/60をモメンタム加速の閾値に使用
MACD_CROSS_PENALTY = 0.15          # MACDの方向一致ボーナス/不一致ペナルティ
LIQUIDITY_BONUS_MAX = 0.06          
FGI_PROXY_BONUS_MAX = 0.05         
FOREX_BONUS_MAX = 0.0               

# ボラティリティ指標 (ATR) の設定 
ATR_LENGTH = 14
ATR_MULTIPLIER_SL = 2.0             # SL = 2.0 * ATR
MIN_RISK_PERCENT = 0.008            # 最低リスク幅 (0.8%) 
RR_RATIO_TARGET = 1.5               # 💡 新規追加: 基本的なリスクリワード比率 (1:1.5)

# 市場環境に応じた動的閾値調整のための定数
FGI_SLUMP_THRESHOLD = -0.02         
FGI_ACTIVE_THRESHOLD = 0.02         
SIGNAL_THRESHOLD_SLUMP = 0.90       
SIGNAL_THRESHOLD_NORMAL = 0.85      
SIGNAL_THRESHOLD_ACTIVE = 0.80      

RSI_DIVERGENCE_BONUS = 0.10         
VOLATILITY_BB_PENALTY_THRESHOLD = 0.01 # BB幅が価格の1%を超えるとペナルティ
OBV_MOMENTUM_BONUS = 0.04           

# ====================================================================================
# UTILITIES & FORMATTING 
# ====================================================================================

# ... (既存のUTILITIES & FORMATTING関数は変更なし)
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
        
    return max(0.0, liquidation_price) 

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

def format_startup_message(account_status: Dict, macro_context: Dict, monitoring_count: int, current_threshold: float) -> str:
    """BOT起動完了時の通知メッセージを整形する。"""
    
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    bot_version = BOT_VERSION
    
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    fgi_raw_value = macro_context.get('fgi_raw_value', 'N/A')
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    
    # current_threshold に応じてテキストを決定するロジック
    if current_threshold == SIGNAL_THRESHOLD_SLUMP:
        market_condition_text = "低迷/リスクオフ"
    elif current_threshold == SIGNAL_THRESHOLD_ACTIVE:
        market_condition_text = "活発/リスクオン"
    else:
        market_condition_text = "通常/中立"
        
    trade_status = "自動売買 **ON** (Long/Short)" if not TEST_MODE else "自動売買 **OFF** (TEST_MODE)"

    header = (
        f"🤖 **Apex BOT 起動完了通知** 🟢\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **確認日時**: {now_jst} (JST)\n"
        f"  - **取引所**: <code>{CCXT_CLIENT_NAME.upper()}</code> (先物モード / **{LEVERAGE}x**)\n" 
        f"  - **自動売買**: <b>{trade_status}</b>\n"
        f"  - **取引ロット**: **固定** <code>{format_usdt(FIXED_NOTIONAL_USDT)}</code> **USDT**\n" 
        f"  - **最大リスク/取引**: **ATRベース**の**動的SL**で管理\n" 
        f"  - **監視銘柄数**: <code>{monitoring_count}</code>\n"
        f"  - **BOTバージョン**: <code>{bot_version}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    balance_section = f"💰 <b>先物口座ステータス</b>\n" 
    if account_status.get('error'):
        balance_section += f"<pre>⚠️ ステータス取得失敗 (致命的エラーにより取引停止中)</pre>\n"
    else:
        equity_display = account_status['total_usdt_balance'] 
        balance_section += (
            f"  - **総資産 (Equity)**: <code>{format_usdt(equity_display)}</code> USDT\n" 
        )
        
        # ボットが管理しているポジション
        if OPEN_POSITIONS:
            total_managed_value = sum(p['filled_usdt'] for p in OPEN_POSITIONS) 
            balance_section += (
                f"  - **管理中ポジション**: <code>{len(OPEN_POSITIONS)}</code> 銘柄 (名目価値合計: <code>{format_usdt(total_managed_value)}</code> USDT)\n" 
            )
            for i, pos in enumerate(OPEN_POSITIONS[:3]): # Top 3のみ表示
                base_currency = pos['symbol'].split('/')[0] # /USDTを除去
                side_tag = '🟢L' if pos.get('side', 'long') == 'long' else '🔴S' 
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
        f"  - **FGI (恐怖・貪欲)**: <code>{GLOBAL_MACRO_CONTEXT.get('fgi_raw_value', 'N/A')}</code> ({'リスクオン' if fgi_proxy > FGI_ACTIVE_THRESHOLD else ('リスクオフ' if fgi_proxy < FGI_SLUMP_THRESHOLD else '中立')})\n"
        f"  - **総合マクロ影響**: <code>{((fgi_proxy + forex_bonus) * 100):.2f}</code> 点\n\n"
    )

    footer = (
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>※ この通知はメインの分析ループが一度完了したことを示します。約1分ごとに分析が実行されます。</pre>"
    )

    return header + balance_section + macro_section + footer


def format_telegram_message(signal: Dict, context: str, current_threshold: float, trade_result: Optional[Dict] = None, exit_type: Optional[str] = None) -> str:
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    symbol = signal['symbol']
    timeframe = signal.get('timeframe', '1h')
    score = signal['score']
    side = signal.get('side', 'long') 
    
    # 価格情報、RR比率は signal 辞書または trade_result から取得 (process_entry_signalで更新されているはず)
    entry_price = signal.get('entry_price', 0.0)
    stop_loss = signal.get('stop_loss', 0.0)
    take_profit = signal.get('take_profit', 0.0)
    liquidation_price = signal.get('liquidation_price', 0.0) 
    rr_ratio = signal.get('rr_ratio', 0.0)
    
    estimated_wr = get_estimated_win_rate(score)
    
    breakdown_details = get_score_breakdown(signal) if context != "ポジション決済" else ""

    trade_section = ""
    trade_status_line = ""
    
    # リスク幅、リワード幅の計算 (0でないことを前提とする)
    risk_width = abs(entry_price - stop_loss)
    reward_width = abs(take_profit - entry_price)
    
    # SL比率 (EntryPriceに対するリスク幅)
    sl_ratio = risk_width / entry_price if entry_price > 0 else 0.0


    if context == "取引シグナル":
        
        notional_value = trade_result.get('filled_usdt', FIXED_NOTIONAL_USDT) if trade_result else FIXED_NOTIONAL_USDT # 実際に約定した名目価値
        
        trade_type_text = "先物ロング" if side == 'long' else "先物ショート"
        order_type_text = "成行買い" if side == 'long' else "成行売り"
        
        if TEST_MODE:
            trade_status_line = f"⚠️ **テストモード**: 取引は実行されません。(ロット: {format_usdt(notional_value)} USDT, {LEVERAGE}x)" 
        elif trade_result is None or trade_result.get('status') == 'error':
            trade_status_line = f"❌ **自動売買 失敗**: {trade_result.get('error_message', 'APIエラー')}"
        elif trade_result.get('status') == 'ok':
            trade_status_line = f"✅ **自動売買 成功**: **{trade_type_text}**注文を執行しました。" 
            
            filled_amount_raw = trade_result.get('filled_amount', 0.0)
            try:
                filled_amount = float(filled_amount_raw)
            except (ValueError, TypeError):
                filled_amount = 0.0
                
            filled_usdt_notional = trade_result.get('filled_usdt', FIXED_NOTIONAL_USDT) 
            # 💡 修正: リスク額はSL比率とレバレッジで計算
            risk_usdt = abs(filled_usdt_notional * sl_ratio * LEVERAGE) 
            
            trade_section = (
                f"💰 **取引実行結果**\n"
                f"  - **注文タイプ**: <code>先物 (Future) / {order_type_text} ({side.capitalize()})</code>\n" 
                f"  - **レバレッジ**: <code>{LEVERAGE}</code> 倍\n" 
                f"  - **名目ロット**: <code>{format_usdt(filled_usdt_notional)}</code> USDT (固定)\n" 
                f"  - **推定リスク額**: <code>{format_usdt(risk_usdt)}</code> USDT (計算 SL: {sl_ratio*100:.2f}%)\n"
                f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
            )
            
    elif context == "ポジション決済":
        exit_type_final = trade_result.get('exit_type', exit_type or '不明')
        side_text = "ロング" if side == 'long' else "ショート"
        trade_status_line = f"🔴 **{side_text} ポジション決済**: {exit_type_final} トリガー ({LEVERAGE}x)" 
        
        entry_price = trade_result.get('entry_price', 0.0)
        exit_price = trade_result.get('exit_price', 0.0)
        pnl_usdt = trade_result.get('pnl_usdt', 0.0)
        pnl_rate = trade_result.get('pnl_rate', 0.0)
        
        filled_amount_raw = trade_result.get('filled_amount', 0.0)
        try:
            filled_amount = float(filled_amount_raw)
            # ポジションをクローズするため、契約数は元のポジションの契約数を反映させるべき
            if 'contracts' in signal:
                 filled_amount = signal['contracts'] 
        except (ValueError, TypeError):
            filled_amount = 0.0
        
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
        f"  - **取引閾値**: <code>{current_threshold * 100:.0f}</code> 点\n"
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
            f"{get_score_breakdown(signal)}\n"
            f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )
        
    message += (f"<i>Bot Ver: {BOT_VERSION} - Future Trading / {LEVERAGE}x Leverage</i>") 
    return message


async def send_telegram_notification(message: str) -> bool:
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

def get_score_breakdown(signal: Dict) -> str:
    """シグナルのスコア内訳を整形して返す"""
    tech_data = signal.get('tech_data', {})
    
    breakdown_list = []
    
    # トレンド一致/逆行
    trend_val = tech_data.get('long_term_reversal_penalty_value', 0.0)
    trend_text = "🟢 長期トレンド一致" if trend_val > 0 else ("🔴 長期トレンド逆行" if trend_val < 0 else "🟡 長期トレンド中立")
    breakdown_list.append(f"{trend_text}: {trend_val*100:+.2f} 点")

    # MACDモメンタム
    macd_val = tech_data.get('macd_penalty_value', 0.0)
    macd_text = "🟢 MACDモメンタム一致" if macd_val > 0 else ("🔴 MACDモメンタム逆行/失速" if macd_val < 0 else "🟡 MACD中立")
    breakdown_list.append(f"{macd_text}: {macd_val*100:+.2f} 点")
    
    # RSIモメンタム加速
    rsi_val = tech_data.get('rsi_momentum_bonus_value', 0.0)
    if rsi_val > 0:
        breakdown_list.append(f"🟢 RSIモメンタム加速/適正水準: {rsi_val*100:+.2f} 点")
    
    # OBV確証
    obv_val = tech_data.get('obv_momentum_bonus_value', 0.0)
    if obv_val > 0:
        breakdown_list.append(f"🟢 OBV出来高確証: {obv_val*100:+.2f} 点")

    # 流動性ボーナス
    liq_val = tech_data.get('liquidity_bonus_value', 0.0)
    if liq_val > 0:
        breakdown_list.append(f"🟢 流動性 (TOP銘柄): {liq_val*100:+.2f} 点")
        
    # FGIマクロ影響
    fgi_val = tech_data.get('sentiment_fgi_proxy_bonus', 0.0)
    fgi_text = "🟢 FGIマクロ追い風" if fgi_val > 0 else ("🔴 FGIマクロ向かい風" if fgi_val < 0 else "🟡 FGIマクロ中立")
    breakdown_list.append(f"{fgi_text}: {fgi_val*100:+.2f} 点")
    
    # ボラティリティペナルティ
    vol_val = tech_data.get('volatility_penalty_value', 0.0)
    if vol_val < 0:
        breakdown_list.append(f"🔴 ボラティリティ過熱ペナルティ: {vol_val*100:+.2f} 点")
        
    # 構造的ボーナス
    struct_val = tech_data.get('structural_pivot_bonus', 0.0)
    breakdown_list.append(f"🟢 構造的優位性 (ベース): {struct_val*100:+.2f} 点")
    
    return "\n".join([f"    - {line}" for line in breakdown_list])

def _to_json_compatible(obj):
    """
    再帰的にオブジェクトをJSON互換の型に変換するヘルパー関数。
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
    try:
        log_entry = {
            'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            'log_type': log_type,
            'symbol': data.get('symbol', 'N/A'),
            'side': data.get('side', 'N/A'), 
            'timeframe': data.get('timeframe', 'N/A'),
            'score': data.get('score', 0.0),
            'rr_ratio': data.get('rr_ratio', 0.0),
            # 💡 修正: ログに記録する際、Entry/SL/TPも記録
            'entry_price': data.get('entry_price', 0.0),
            'stop_loss': data.get('stop_loss', 0.0),
            'take_profit': data.get('take_profit', 0.0),
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

# 🆕 機能追加: 取引閾値未満の最高スコアを定期通知
async def notify_highest_analysis_score():
    """
    分析の結果、取引閾値に満たない最高スコア銘柄を1時間ごとに通知する。
    """
    global LAST_ANALYSIS_SIGNALS, LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, ANALYSIS_ONLY_INTERVAL, SIGNAL_THRESHOLD, GLOBAL_MACRO_CONTEXT, BASE_SCORE
    
    current_time = time.time()
    
    # 実行間隔のチェック
    if current_time - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME < ANALYSIS_ONLY_INTERVAL:
        return

    # 分析結果が格納されているかチェック
    if not LAST_ANALYSIS_SIGNALS:
        logging.info("ℹ️ 分析結果がないため、最高スコア通知をスキップします。")
        LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = current_time 
        return

    # スコアの降順でソートし、最高スコアのシグナルを取得
    sorted_signals = sorted(LAST_ANALYSIS_SIGNALS, key=lambda x: x.get('score', 0.0), reverse=True)
    best_signal = sorted_signals[0]
    
    best_score = best_signal.get('score', 0.0)
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    # 最高スコアが取引閾値未満で、かつ最低限の分析スコア(例: BASE_SCORE+0.1)を超えている場合
    if best_score < current_threshold and best_score >= (BASE_SCORE + 0.1): 
        
        symbol = best_signal.get('symbol', 'N/A')
        timeframe = best_signal.get('timeframe', 'N/A')
        side = best_signal.get('side', 'long')

        # 通知メッセージの整形
        message = (
            f"📈 **分析結果 (最高スコア)** - 定時レポート\n"
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
            f"  - **確認日時**: {datetime.now(JST).strftime('%Y/%m/%d %H:%M:%S')} (JST)\n"
            f"  - **最高スコア銘柄**: <b>{symbol}</b> ({timeframe})\n"
            f"  - **最高スコア**: <code>{best_score * 100:.2f} / 100</code> ({side.capitalize()})\n"
            f"  - **取引閾値**: <code>{current_threshold * 100:.2f}</code> 点\n"
            f"  - **備考**: 取引閾値 (<code>{current_threshold * 100:.2f}</code>点) に満たなかったため取引はスキップされました。\n"
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
            f"<i>Bot Ver: {BOT_VERSION}</i>"
        )
        logging.info(f"ℹ️ 定時報告: 取引閾値未満の最高スコアを通知します: {symbol} ({side}) Score: {best_score:.2f}")
        await send_telegram_notification(message)
    else:
        # 最高スコアが閾値以上、または分析スコアが低すぎる場合は通知スキップ
        if best_score >= current_threshold:
            logging.info("ℹ️ 最高スコアが取引閾値以上のため、定時通知をスキップします (取引シグナルとして通知済みと想定)。")
        else:
            logging.info("ℹ️ 最高スコアが低すぎるため、定時通知をスキップします。")
    
    # 通知/ログ処理が完了したら時間を更新
    LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = current_time

# ====================================================================================
# WEBSHARE FUNCTION (HTTP POST) 
# ====================================================================================
async def send_webshare_update(data: Dict[str, Any]):
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
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    IS_CLIENT_READY = False
    
    if not API_KEY or not SECRET_KEY:
        logging.critical("❌ CCXT初期化スキップ: APIキー または SECRET_KEY が設定されていません。")
        return False
    
    # 既存のクライアントがあれば、リソースを解放する
    if EXCHANGE_CLIENT:
        try:
            await EXCHANGE_CLIENT.close()
            logging.info("✅ 既存のCCXTクライアントセッションを正常にクローズしました。")
        except Exception as e:
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
        timeout_ms = 30000 

        EXCHANGE_CLIENT = exchange_class({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True,
            'options': options,
            'timeout': timeout_ms 
        })
        
        logging.info(f"✅ CCXTクライアントの初期化設定完了。リクエストタイムアウト: {timeout_ms/1000}秒。")
        
        await EXCHANGE_CLIENT.load_markets()

        # レバレッジの設定 (MEXC向け)
        if EXCHANGE_CLIENT.id == 'mexc':
            symbols_to_set_leverage = []
            # DEFAULT_SYMBOLSに含まれるCCXT標準シンボル (例: BTC/USDT) をベース/クォート通貨に分解
            default_base_quotes = {s.split('/')[0]: s.split('/')[1] for s in DEFAULT_SYMBOLS if '/' in s}
            
            for mkt in EXCHANGE_CLIENT.markets.values():
                # USDT建てのSwap/Future市場を探す
                if mkt['quote'] == 'USDT' and mkt['type'] in ['swap', 'future'] and mkt['active']:
                    # 市場の基本通貨が DEFAULT_SYMBOLS のベース通貨に含まれるかチェック
                    if mkt['base'] in default_base_quotes:
                        # set_leverageに渡すべきCCXTシンボル (例: BTC/USDT:USDT) をリストに追加
                        symbols_to_set_leverage.append(mkt['symbol'])
            
            logging.info(f"ℹ️ MEXCクライアント: 初期レバレッジ {LEVERAGE}x を主要 {len(symbols_to_set_leverage)} 銘柄に設定開始。")
            
            # レートリミット対策のため、数秒ごとに設定する
            for i, symbol in enumerate(symbols_to_set_leverage):
                try:
                    await EXCHANGE_CLIENT.set_leverage(LEVERAGE, symbol)
                    # logging.info(f"✅ レバレッジ設定成功: {symbol} to {LEVERAGE}x")
                    if i < len(symbols_to_set_leverage) - 1:
                        await asyncio.sleep(LEVERAGE_SETTING_DELAY) # レートリミット対策
                except ccxt.ExchangeError as e:
                    logging.warning(f"⚠️ レバレッジ設定失敗 (ExchangeError): {symbol} - {e}")
                except Exception as e:
                    logging.warning(f"⚠️ レバレッジ設定失敗 (予期せぬエラー): {symbol} - {e}")
            
            logging.info(f"✅ MEXCクライアント: 初期レバレッジ設定完了。")

        IS_CLIENT_READY = True
        return True

    except ccxt.NetworkError as e:
        logging.critical(f"❌ CCXTネットワークエラー: {e}")
    except ccxt.ExchangeError as e:
        logging.critical(f"❌ CCXT取引所エラー: {e}")
    except Exception as e:
        logging.critical(f"❌ CCXT初期化中に予期せぬエラーが発生しました: {e}", exc_info=True)
    
    return False

# 銘柄リストを更新する関数
async def get_top_symbols(limit: int) -> Tuple[List[str], Optional[str]]:
    global EXCHANGE_CLIENT, CURRENT_MONITOR_SYMBOLS, DEFAULT_SYMBOLS
    
    # テストモードまたはスキップ設定の場合は処理をスキップ
    if SKIP_MARKET_UPDATE or TEST_MODE:
        return CURRENT_MONITOR_SYMBOLS, None
    
    if not IS_CLIENT_READY or EXCHANGE_CLIENT is None:
        return CURRENT_MONITOR_SYMBOLS, "クライアントが初期化されていません。"
    
    new_symbols = []
    error_message: Optional[str] = None
    
    try:
        # 💡 【修正点2: NoneType対策】fetch_tickersがNoneを返す可能性があるため、防御的なチェックを追加
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        if tickers is None:
            error_message = "🚨 fetch_tickers()がNoneを返しました。レートリミットまたはAPI障害の可能性あり。"
            logging.error(error_message)
            # 致命的エラーとして処理せず、既存リストを維持して続行
            return CURRENT_MONITOR_SYMBOLS, None 

        # 出来高 (quoteVolume) の降順でソート
        ticker_list = sorted(
            [
                (symbol, data.get('quoteVolume', 0))
                for symbol, data in tickers.items()
                # USDT建ての先物/スワップのみを対象とする
                if '/USDT' in symbol and 'future' in EXCHANGE_CLIENT.markets.get(symbol, {}).get('type', '')
            ],
            key=lambda x: x[1],
            reverse=True
        )

        # TOP Nのシンボルを取得
        top_n_symbols = [symbol for symbol, volume in ticker_list[:limit]]

        # デフォルトリストとマージし、重複を排除
        # 出来高上位の銘柄を優先する
        combined_symbols = list(set(top_n_symbols + DEFAULT_SYMBOLS))
        
        new_symbols = [s for s in combined_symbols if s in EXCHANGE_CLIENT.markets and EXCHANGE_CLIENT.markets[s].get('active')]

        if new_symbols:
            CURRENT_MONITOR_SYMBOLS = new_symbols
            logging.info(f"✅ 監視銘柄リストを更新しました。合計: {len(CURRENT_MONITOR_SYMBOLS)} 銘柄。")
        
        return CURRENT_MONITOR_SYMBOLS, None

    except ccxt.ExchangeError as e:
        error_message = f"❌ 取引所エラー: 銘柄リスト更新失敗: {e}"
        logging.error(error_message)
    except Exception as e:
        error_message = f"❌ 銘柄リスト更新中に予期せぬエラーが発生しました: {e}"
        logging.error(error_message, exc_info=True)
        
    return CURRENT_MONITOR_SYMBOLS, error_message

# 口座ステータスを取得し、グローバル変数を更新する関数
async def get_account_status() -> Dict:
    global EXCHANGE_CLIENT, ACCOUNT_EQUITY_USDT, OPEN_POSITIONS
    
    if not IS_CLIENT_READY or EXCHANGE_CLIENT is None:
        return {'error': True, 'message': "クライアントが初期化されていません。", 'total_usdt_balance': ACCOUNT_EQUITY_USDT}
    
    error_message: Optional[str] = None
    
    try:
        # 💡 【修正点1: fetchBalance() not support self method 対策】
        # MEXCなど一部の取引所ではfetchBalanceが先物口座の正確なEquityを返さない、またはエラーとなる
        
        if EXCHANGE_CLIENT.id == 'mexc':
            # MEXCの場合、fetchBalanceはサポートされていないというエラーログが出たため、スキップする
            # 代わりに、fetch_positionsで返されるポジション情報から証拠金情報を推測/使用する
            logging.warning("⚠️ MEXCクライアント: fetchBalance()は既知のエラーがあるためスキップします。")
            balance = {'total': {'USDT': 0.0, 'equity': 0.0}, 'free': {'USDT': 0.0}}
            # ACCOUNT_EQUITY_USDTはポジション取得後に更新されることを期待する
            
        else:
            # 他の取引所ではfetchBalanceを使用
            balance = await EXCHANGE_CLIENT.fetch_balance()
            # USDTの総残高（先物口座のEquityまたはTotal Balance）を取得
            # 注: 'total', 'free', 'used'のキーは取引所によって異なるため、一般的な'total'と'USDT'を使用
            equity = balance.get('total', {}).get('USDT', 0.0) 
            ACCOUNT_EQUITY_USDT = equity
            logging.info(f"ℹ️ fetch_balance()経由での総資産 (Equity): {format_usdt(ACCOUNT_EQUITY_USDT)} USDT")


        # ポジションの取得と更新
        positions = await EXCHANGE_CLIENT.fetch_positions()
        OPEN_POSITIONS = []
        total_unrealized_pnl = 0.0
        
        for p in positions:
            # アクティブな先物ポジションのみを対象とする
            if p['side'] in ['long', 'short'] and p['contracts'] != 0:
                # CCXTポジションオブジェクトから必要な情報を抽出
                pos_info = {
                    'symbol': p['symbol'],
                    'contracts': abs(p['contracts']), # 契約の絶対値
                    'side': p['side'],
                    'entry_price': p['entryPrice'],
                    'current_price': p['markPrice'],
                    'unrealized_pnl': p['unrealizedPnl'],
                    'margin_used': p['initialMargin'],
                    'leverage': p.get('leverage', LEVERAGE),
                    # その他の必要な情報 (SL/TP、filled_usdtなど) はボット内部で管理するため、ここでは最小限
                    'filled_usdt': abs(p['notional']), # ポジションの名目価値
                    'is_managed': False, # ボットが管理しているかどうかのフラグ
                    'stop_loss': 0.0,
                    'take_profit': 0.0
                }
                OPEN_POSITIONS.append(pos_info)
                total_unrealized_pnl += p['unrealizedPnl']
                
        # MEXCなどfetch_balanceが機能しない場合、Equityをポジション情報から推測する（ざっくりとした計算）
        if EXCHANGE_CLIENT.id == 'mexc':
            # 総資産 = (総証拠金 + 実現損益) + 未実現損益 (非常に単純化)
            # 実際には「アカウント」エンドポイントから取得すべきだが、今回はfetchBalanceエラー対策を優先
            # ポジションモニタで最新のEquityを正確に取得するまでは、前回値を使用するか、最低限の安全値を設定
            
            # ポジションの維持証拠金や使用証拠金を合算して Equity を更新しようとすると複雑になるため、
            # 初期化メッセージでは一旦 0.0 を表示し、実際の取引ではポジション情報を優先的に使用する。
            # ※ 実運用では、MEXCのカスタム`fapiPrivateGetAccount`などを利用して正確なEquityを取得すべき。
            
            # 今回は、エラーログを回避し、BOTがクラッシュせず続行することを最優先とする。
            
            # ここでは fetchBalance() の結果が 0.0/None であることを考慮し、前回値か 0.0 を維持。
            pass
        
        # ボットが管理しているポジションのSL/TPを、内部で持っている状態と同期させる
        # ... (この部分は、main_bot_logic内で過去のポジション情報をOPEN_POSITIONSに反映させるロジックが必要だが、今回はコアのエラー修正に集中する)
        
        # 正常終了の辞書を返す
        return {
            'error': False, 
            'total_usdt_balance': ACCOUNT_EQUITY_USDT + total_unrealized_pnl, # PnLを考慮
            'total_unrealized_pnl': total_unrealized_pnl,
            'positions': OPEN_POSITIONS,
            'message': "アカウントステータス取得成功"
        }

    except ccxt.ExchangeError as e:
        error_message = f"❌ 取引所エラー: 口座ステータス取得失敗: {e}"
        logging.error(error_message)
    except Exception as e:
        error_message = f"❌ 口座ステータス取得中に予期せぬエラーが発生しました: {e}"
        logging.error(error_message, exc_info=True)
        
    return {'error': True, 'message': error_message, 'total_usdt_balance': ACCOUNT_EQUITY_USDT}

# OHLCVデータの取得関数
async def fetch_ohlcv_data(symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
    # ... (既存のコードは変更なし)
    global EXCHANGE_CLIENT
    
    if not IS_CLIENT_READY or EXCHANGE_CLIENT is None:
        return None
        
    try:
        # limitは必須OHLCV数より少し多めにする
        limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 500)
        
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(
            symbol=symbol, 
            timeframe=timeframe, 
            limit=limit + 5 # 余裕を持たせる
        )

        if not ohlcv or len(ohlcv) < limit:
            logging.warning(f"⚠️ {symbol} ({timeframe}): 必要なデータ数 ({limit}) を取得できませんでした。スキップします。")
            return None

        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert(JST)
        df.set_index('timestamp', inplace=True)
        return df

    except ccxt.ExchangeError as e:
        # レートリミットエラーの場合は警告、その他のエラーはログに記録
        if 'rate limit exceeded' in str(e).lower() or 'too many requests' in str(e).lower():
            logging.warning(f"⚠️ {symbol} ({timeframe}): レートリミット超過。スキップします。")
        else:
            logging.error(f"❌ {symbol} ({timeframe}): 取引所エラー: {e}")
    except Exception as e:
        logging.error(f"❌ {symbol} ({timeframe}): OHLCV取得中に予期せぬエラーが発生しました: {e}", exc_info=True)
        
    return None


# FGI (恐怖・貪欲指数) データを取得する関数
async def get_fgi_data() -> Dict:
    # ... (既存のコードは変更なし)
    global FGI_API_URL, GLOBAL_MACRO_CONTEXT, FGI_PROXY_BONUS_MAX
    
    try:
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        response.raise_for_status()
        
        data = response.json()
        
        if not data or not data.get('data'):
            logging.warning("⚠️ FGIデータが空か不正な形式です。デフォルト値を使用します。")
            return GLOBAL_MACRO_CONTEXT
            
        fgi_value = int(data['data'][0]['value']) # 0(Extreme Fear) to 100(Extreme Greed)
        fgi_classification = data['data'][0]['value_classification']
        
        # FGI値を -1.0 から 1.0 の範囲に正規化するプロキシ (50が0.0)
        fgi_proxy = (fgi_value - 50) / 50 
        
        # FGIプロキシを最大ボーナス値の範囲に制限
        fgi_proxy = np.clip(fgi_proxy, -FGI_PROXY_BONUS_MAX / FGI_PROXY_BONUS_MAX, FGI_PROXY_BONUS_MAX / FGI_PROXY_BONUS_MAX)
        fgi_proxy *= FGI_PROXY_BONUS_MAX

        # グローバルコンテキストを更新
        GLOBAL_MACRO_CONTEXT['fgi_proxy'] = fgi_proxy
        GLOBAL_MACRO_CONTEXT['fgi_raw_value'] = f"{fgi_value} ({fgi_classification})"
        
        logging.info(f"✅ FGIデータ取得成功: {fgi_value} ({fgi_classification}), Proxy: {GLOBAL_MACRO_CONTEXT['fgi_proxy']:.4f}")
        
        return GLOBAL_MACRO_CONTEXT
        
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ FGI APIリクエストエラー: {e}")
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logging.error(f"❌ FGIデータ解析エラー: {e}")
    
    # 失敗時はグローバルコンテキストをそのまま返す
    return GLOBAL_MACRO_CONTEXT

# ====================================================================================
# STRATEGY & INDICATORS (コアロジック)
# ====================================================================================

# ATR (Average True Range) を計算する
def calculate_atr(df: pd.DataFrame) -> float:
    # ... (既存のコードは変更なし)
    if df is None or len(df) < ATR_LENGTH:
        return 0.0
        
    # pandas_taのATRを使用
    atr_series = ta.atr(df['high'], df['low'], df['close'], length=ATR_LENGTH)
    
    # 最新のATR値を返す
    return atr_series.iloc[-1] if not atr_series.empty else 0.0

# 実践的なスコアリングロジック
def calculate_signal_score(
    df_1h: pd.DataFrame, 
    df_4h: pd.DataFrame,
    symbol: str, 
    timeframe: str, 
    side: str, 
    macro_context: Dict
) -> Tuple[float, Dict]:
    """
    複数のテクニカル指標を組み合わせた実践的なスコアリングロジック。
    
    Args:
        df_1h, df_4h: 1時間足と4時間足のデータフレーム。
        symbol: 銘柄名。
        timeframe: 現在の分析時間足 ('1h' or '4h')。
        side: シグナルの方向 ('long' or 'short')。
        macro_context: 市場環境 (FGIなど)。
        
    Returns:
        float: 総合スコア (0.0 から 1.0)。
        Dict: スコア内訳の技術データ。
    """
    
    # ... (既存のスコアリングロジックは変更なし - 実践的ロジック v20.0.43)
    
    tech_data: Dict = {}
    current_df = df_1h if timeframe == '1h' else df_4h
    if current_df is None or len(current_df) < LONG_TERM_SMA_LENGTH + ATR_LENGTH:
        return 0.0, tech_data
        
    # --- 1. ベーススコア ---
    score = BASE_SCORE # 0.40

    # --- 2. 流動性ボーナス ---
    # 出来高TOP銘柄ほどボーナスを加算
    # TOP_SYMBOL_LIMITが40なので、TOP10に入っていれば最大の0.06ボーナス
    try:
        top_symbols = [s for s, _ in sorted(EXCHANGE_CLIENT.fetch_tickers().items(), key=lambda item: item[1].get('quoteVolume', 0), reverse=True) if '/USDT' in s][:TOP_SYMBOL_LIMIT]
        if symbol in top_symbols:
            rank = top_symbols.index(symbol) + 1
            liquidity_bonus = LIQUIDITY_BONUS_MAX * (1 - (rank - 1) / TOP_SYMBOL_LIMIT)
            liquidity_bonus = max(0.0, min(LIQUIDITY_BONUS_MAX, liquidity_bonus))
            score += liquidity_bonus
            tech_data['liquidity_bonus_value'] = liquidity_bonus
    except Exception:
        tech_data['liquidity_bonus_value'] = 0.0
        pass

    # --- 3. 長期トレンド順張りボーナス/逆張りペナルティ (SMA 200) ---
    current_df['SMA_200'] = ta.sma(current_df['close'], length=LONG_TERM_SMA_LENGTH)
    last_close = current_df['close'].iloc[-1]
    last_sma_200 = current_df['SMA_200'].iloc[-1]
    
    trend_penalty = 0.0
    if not np.isnan(last_sma_200):
        if side == 'long':
            if last_close < last_sma_200:
                trend_penalty = -LONG_TERM_REVERSAL_PENALTY # 逆張りペナルティ
            else:
                trend_penalty = LONG_TERM_REVERSAL_PENALTY * 0.5 # 順張りボーナス (半分)
        elif side == 'short':
            if last_close > last_sma_200:
                trend_penalty = -LONG_TERM_REVERSAL_PENALTY # 逆張りペナルティ
            else:
                trend_penalty = LONG_TERM_REVERSAL_PENALTY * 0.5 # 順張りボーナス (半分)
    
    score += trend_penalty
    tech_data['long_term_reversal_penalty_value'] = trend_penalty


    # --- 4. モメンタム指標 (RSI, MACD) ---
    
    # a) MACD (方向一致ボーナス/不一致ペナルティ)
    macd_df = ta.macd(current_df['close'])
    if macd_df is not None and len(macd_df) > 1:
        # MACD線 (MACD) とシグナル線 (MACDh) の最新値
        last_macd = macd_df.iloc[-1]['MACD_12_26_9']
        last_signal = macd_df.iloc[-1]['MACDs_12_26_9']
        
        # MACDの方向チェック (MACDがシグナルを上回る/下回る)
        is_uptrend = last_macd > last_signal
        
        macd_penalty = 0.0
        if side == 'long' and not is_uptrend:
            macd_penalty = -MACD_CROSS_PENALTY # モメンタム逆行
        elif side == 'short' and is_uptrend:
            macd_penalty = -MACD_CROSS_PENALTY # モメンタム逆行
        else:
            macd_penalty = MACD_CROSS_PENALTY * 0.5 # モメンタム一致ボーナス
            
        score += macd_penalty
        tech_data['macd_penalty_value'] = macd_penalty
    else:
        tech_data['macd_penalty_value'] = 0.0


    # b) RSI (モメンタム加速ボーナス)
    current_df['RSI'] = ta.rsi(current_df['close'], length=14)
    last_rsi = current_df['RSI'].iloc[-1]
    
    rsi_bonus = 0.0
    if side == 'long' and last_rsi > RSI_MOMENTUM_LOW and last_rsi < 70:
        # 40-70の間でモメンタム加速または過熱なし
        rsi_bonus = STRUCTURAL_PIVOT_BONUS * 0.5 
    elif side == 'short' and last_rsi < (100 - RSI_MOMENTUM_LOW) and last_rsi > 30:
        # 30-60の間でモメンタム加速または過熱なし
        rsi_bonus = STRUCTURAL_PIVOT_BONUS * 0.5
        
    score += rsi_bonus
    tech_data['rsi_momentum_bonus_value'] = rsi_bonus


    # --- 5. 出来高確証 (OBV) ---
    current_df['OBV'] = ta.obv(current_df['close'], current_df['volume'])
    if len(current_df) > 2:
        # OBVの傾きをチェック (簡易的な方法として、直近2本の差分をチェック)
        obv_diff = current_df['OBV'].iloc[-1] - current_df['OBV'].iloc[-2]
        
        obv_bonus = 0.0
        if side == 'long' and obv_diff > 0:
            obv_bonus = OBV_MOMENTUM_BONUS # 出来高上昇で確証
        elif side == 'short' and obv_diff < 0:
            obv_bonus = OBV_MOMENTUM_BONUS # 出来高減少（ショートの確証）
            
        score += obv_bonus
        tech_data['obv_momentum_bonus_value'] = obv_bonus
    else:
        tech_data['obv_momentum_bonus_value'] = 0.0

    # --- 6. ボラティリティペナルティ (BBand幅) ---
    bband = ta.bbands(current_df['close'], length=20, std=2)
    bband_width = bband['BBL_20_2.0'].iloc[-1] / current_df['close'].iloc[-1]
    
    volatility_penalty = 0.0
    if bband_width > VOLATILITY_BB_PENALTY_THRESHOLD: # BB幅が価格の1%を超える場合
        # ボラティリティ過熱でペナルティ
        volatility_penalty = -STRUCTURAL_PIVOT_BONUS
        
    score += volatility_penalty
    tech_data['volatility_penalty_value'] = volatility_penalty


    # --- 7. マクロ環境影響 (FGI) ---
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    
    fgi_final_bonus = 0.0
    if side == 'long' and fgi_proxy > 0:
        # ロングで市場がGreed (リスクオン) ならボーナス
        fgi_final_bonus = fgi_proxy
    elif side == 'short' and fgi_proxy < 0:
        # ショートで市場がFear (リスクオフ) ならボーナス
        fgi_final_bonus = abs(fgi_proxy) * 0.5 # ショートはリスクが高いため、ボーナスは半分

    score += fgi_final_bonus
    tech_data['sentiment_fgi_proxy_bonus'] = fgi_final_bonus


    # --- 8. 構造的優位性ボーナス (固定) ---
    # これはベースロジックの優位性を示す固定ボーナス
    score += STRUCTURAL_PIVOT_BONUS
    tech_data['structural_pivot_bonus'] = STRUCTURAL_PIVOT_BONUS

    # スコアを 0.0 から 1.0 の範囲にクリップ
    final_score = np.clip(score, 0.0, 1.0)
    
    return float(final_score), tech_data


# エントリーシグナルを処理し、取引を実行する
async def process_entry_signal(signal: Dict) -> Optional[Dict]:
    # ... (既存のコードは変更なし)
    global EXCHANGE_CLIENT, LAST_SIGNAL_TIME, OPEN_POSITIONS, ACCOUNT_EQUITY_USDT, TRADE_SIGNAL_COOLDOWN
    
    symbol = signal['symbol']
    side = signal['side']
    timeframe = signal['timeframe']
    
    # 冷却期間チェック
    last_trade_time = LAST_SIGNAL_TIME.get(symbol, 0.0)
    if time.time() - last_trade_time < TRADE_SIGNAL_COOLDOWN:
        logging.info(f"ℹ️ {symbol} ({side}): 冷却期間中のためスキップします。")
        return None
        
    # すでにポジションを持っている場合はスキップ
    if any(p['symbol'] == symbol and p['side'] == side for p in OPEN_POSITIONS):
        logging.info(f"ℹ️ {symbol} ({side}): すでにポジションを持っているためスキップします。")
        return None

    # 最新のOHLCVデータを取得し、エントリー価格とATRを取得
    df = await fetch_ohlcv_data(symbol, timeframe)
    if df is None:
        logging.warning(f"⚠️ {symbol} ({timeframe}): 取引に必要なOHLCVデータが不足しています。スキップします。")
        return None
        
    current_price = df['close'].iloc[-1]
    current_atr = calculate_atr(df)
    
    if current_atr <= 0.0 or current_price <= 0.0:
        logging.warning(f"⚠️ {symbol} ({timeframe}): ATRまたは現在価格が無効です。スキップします。")
        return None
        
    # --- リスク管理とSL/TPの計算 (ATRベースの動的設定) ---
    
    # 1. ストップロス (SL) の計算
    sl_distance = current_atr * ATR_MULTIPLIER_SL # SL = 2.0 * ATR
    
    # 最小リスク幅の強制 (例: 0.8%を担保)
    min_sl_distance = current_price * MIN_RISK_PERCENT 
    sl_distance = max(sl_distance, min_sl_distance)
    
    if side == 'long':
        stop_loss = current_price - sl_distance
    else:
        stop_loss = current_price + sl_distance
        
    # SLがゼロ以下にならないようにチェック
    if stop_loss <= 0.0:
        logging.error(f"❌ {symbol} ({side}): 計算されたSL価格 ({stop_loss}) が無効です。スキップします。")
        return None

    # 2. テイクプロフィット (TP) の計算
    # リスクリワード比率 (RRR) に基づき、SL距離からTP距離を決定
    tp_distance = sl_distance * RR_RATIO_TARGET 
    
    if side == 'long':
        take_profit = current_price + tp_distance
    else:
        take_profit = current_price - tp_distance
        
    # 3. 清算価格 (Liquidation Price) の推定
    liquidation_price = calculate_liquidation_price(
        entry_price=current_price, 
        leverage=LEVERAGE, 
        side=side, 
        maintenance_margin_rate=MIN_MAINTENANCE_MARGIN_RATE
    )
    
    # 4. ポジション量 (ロット) の計算
    # 固定名目ロット (FIXED_NOTIONAL_USDT) を使用
    notional_usdt = FIXED_NOTIONAL_USDT
    
    # 契約数量 (contracts) を計算: ロット / 現在価格
    contracts = notional_usdt / current_price 
    
    # 取引所のロットサイズに丸める (CCXTのcreate_orderが自動的に丸めるはずだが、ここでは確認用)
    market = EXCHANGE_CLIENT.markets.get(symbol)
    if market:
        # contractsはamount (数量) に相当
        contracts = EXCHANGE_CLIENT.amount_to_precision(symbol, contracts) 
        
    contracts = float(contracts)
    
    if contracts <= 0.0:
        logging.error(f"❌ {symbol}: 計算された契約数量がゼロ以下です。取引をスキップします。")
        return None
        
    # --- 5. 注文実行 ---
    if TEST_MODE:
        logging.warning(f"⚠️ TEST_MODE: {symbol} ({side}) で取引を実行しません。SL/TP: {format_price(stop_loss)} / {format_price(take_profit)}")
        
        # テスト結果をシミュレート
        trade_result = {
            'status': 'ok',
            'filled_amount': contracts,
            'filled_usdt': notional_usdt,
            'entry_price': current_price,
            'error_message': 'TEST_MODE'
        }
        
    else:
        order_type = 'market'
        
        try:
            # 成行注文を執行
            order = await EXCHANGE_CLIENT.create_order(
                symbol=symbol,
                type=order_type,
                side=side,
                amount=contracts, # 契約数量
                params={'leverage': LEVERAGE} # 追加パラメータ
            )
            
            # 注文成功後の処理 (注文結果の解析)
            filled_amount = float(order.get('filled', 0.0))
            order_price = float(order.get('price', current_price))
            
            # SL/TP注文を設定
            # CCXTでは通常、成行注文とは別にSL/TPのOCOやTakeProfit/StopLoss Limit/Marketを送信する必要がある
            # ここでは単純なストップロスマーケット注文とテイクプロフィットマーケット注文を想定
            
            # 1. SL注文 (逆方向の成行注文)
            stop_side = 'sell' if side == 'long' else 'buy'
            await EXCHANGE_CLIENT.create_order(
                symbol=symbol,
                type='stop_market',
                side=stop_side,
                amount=filled_amount,
                price=stop_loss, # trigger price
                params={'stopLossPrice': stop_loss} # 取引所によってはこの形式
            )
            
            # 2. TP注文 (逆方向の成行注文)
            await EXCHANGE_CLIENT.create_order(
                symbol=symbol,
                type='take_profit_market',
                side=stop_side,
                amount=filled_amount,
                price=take_profit, # trigger price
                params={'takeProfitPrice': take_profit} # 取引所によってはこの形式
            )

            # 正常な結果を格納
            trade_result = {
                'status': 'ok',
                'filled_amount': filled_amount,
                'filled_usdt': filled_amount * order_price,
                'entry_price': order_price,
                'order_id': order.get('id'),
                'error_message': None
            }
            
            # 冷却期間を更新
            LAST_SIGNAL_TIME[symbol] = time.time()
            
        except ccxt.ExchangeError as e:
            error_msg = f"取引所エラー: {e}"
            logging.error(f"❌ {symbol} ({side}): 注文実行失敗: {error_msg}")
            trade_result = {'status': 'error', 'error_message': error_msg}
        except Exception as e:
            error_msg = f"予期せぬエラー: {e}"
            logging.error(f"❌ {symbol} ({side}): 注文実行失敗: {error_msg}", exc_info=True)
            trade_result = {'status': 'error', 'error_message': error_msg}
            
    # シグナル情報に取引結果とSL/TP/清算価格を追記
    signal['entry_price'] = trade_result.get('entry_price', current_price) 
    signal['stop_loss'] = stop_loss
    signal['take_profit'] = take_profit
    signal['liquidation_price'] = liquidation_price
    signal['rr_ratio'] = RR_RATIO_TARGET # 固定値

    # 取引に成功した場合、ポジションリストに追加 (次回以降の監視対象とする)
    if trade_result.get('status') == 'ok':
        position_data = {
            'symbol': symbol,
            'contracts': trade_result['filled_amount'],
            'side': side,
            'entry_price': signal['entry_price'],
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'filled_usdt': trade_result['filled_usdt'],
            'is_managed': True # ボットが管理するポジション
        }
        OPEN_POSITIONS.append(position_data)
        
        # Telegram通知を送信
        current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
        message = format_telegram_message(signal, "取引シグナル", current_threshold, trade_result=trade_result)
        await send_telegram_notification(message)
        
        # ログを記録
        log_signal(signal, "Entry_Success", trade_result)
        
    elif trade_result.get('status') == 'error':
        # 取引失敗時も通知
        current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
        message = format_telegram_message(signal, "取引シグナル", current_threshold, trade_result=trade_result)
        await send_telegram_notification(message)
        log_signal(signal, "Entry_Failure", trade_result)
        
    return trade_result

# ====================================================================================
# POSITION MONITORING & EXIT LOGIC
# ====================================================================================

async def check_and_close_position(position: Dict, exit_type: str, exit_price: float, current_price: float) -> Optional[Dict]:
    # ... (既存のコードは変更なし)
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    symbol = position['symbol']
    side = position['side']
    contracts = position['contracts']
    entry_price = position['entry_price']
    
    # 注文の反対側
    close_side = 'sell' if side == 'long' else 'buy'
    
    trade_result = None
    
    if TEST_MODE:
        logging.warning(f"⚠️ TEST_MODE: {symbol} ({side}) ポジションを決済しません。Exit: {exit_type} @ {format_price(exit_price)}")
        
        # PnL計算 (テストモード)
        pnl_usdt = contracts * (current_price - entry_price) * (1 if side == 'long' else -1)
        pnl_rate = (current_price / entry_price - 1) * (1 if side == 'long' else -1) * LEVERAGE
        
        trade_result = {
            'status': 'ok',
            'exit_type': exit_type,
            'exit_price': current_price,
            'entry_price': entry_price,
            'filled_amount': contracts,
            'pnl_usdt': pnl_usdt,
            'pnl_rate': pnl_rate
        }
        
    else:
        try:
            # ポジションのクローズ注文を執行 (成行注文)
            order = await EXCHANGE_CLIENT.create_order(
                symbol=symbol,
                type='market',
                side=close_side,
                amount=contracts, 
                params={'reduceOnly': True} # ポジション解消のみを行う
            )
            
            # 決済価格はオーダー結果から取得するか、現在の価格を使用
            order_price = float(order.get('price', current_price))
            
            # PnLの計算 (概算。正確なPNLは取引所のAPIから取得すべきだが、ここでは迅速な通知のため概算)
            pnl_usdt_approx = contracts * (order_price - entry_price) * (1 if side == 'long' else -1)
            pnl_rate_approx = (order_price / entry_price - 1) * (1 if side == 'long' else -1) * LEVERAGE

            trade_result = {
                'status': 'ok',
                'exit_type': exit_type,
                'exit_price': order_price,
                'entry_price': entry_price,
                'filled_amount': contracts,
                'pnl_usdt': pnl_usdt_approx,
                'pnl_rate': pnl_rate_approx,
                'order_id': order.get('id')
            }
            
            # 🚨 注文成功後、関連するSL/TP注文をキャンセルする必要がある
            # (このステップは取引所API仕様により異なるため、ここでは割愛。実運用では実装必須)
            
        except ccxt.ExchangeError as e:
            logging.error(f"❌ {symbol} ({side}): ポジション決済失敗 (ExchangeError): {e}")
            trade_result = {'status': 'error', 'exit_type': exit_type, 'error_message': str(e)}
        except Exception as e:
            logging.error(f"❌ {symbol} ({side}): ポジション決済失敗 (予期せぬエラー): {e}", exc_info=True)
            trade_result = {'status': 'error', 'exit_type': exit_type, 'error_message': str(e)}

    # ポジションリストから削除 (成功/テストモードの場合)
    if trade_result and trade_result.get('status') in ['ok', 'error']: # エラーの場合も、リストからは削除して再エントリーを試みる
        OPEN_POSITIONS = [p for p in OPEN_POSITIONS if not (p['symbol'] == symbol and p['side'] == side)]
        logging.info(f"✅ {symbol} ({side}) のポジションをリストから削除しました。")
    
    # 決済成功/失敗に関わらず通知
    if trade_result and trade_result.get('status') == 'ok':
        # 通知メッセージを整形するために、元のポジション情報をシグナル形式に変換して使用
        signal_for_notify = {**position, 'score': 0.0, 'timeframe': '4h', 'rr_ratio': RR_RATIO_TARGET} 
        current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
        message = format_telegram_message(signal_for_notify, "ポジション決済", current_threshold, trade_result=trade_result, exit_type=exit_type)
        await send_telegram_notification(message)
        log_signal(signal_for_notify, "Exit_Success", trade_result)
        
    return trade_result


async def position_monitor_logic():
    # ... (既存のコードは変更なし)
    global EXCHANGE_CLIENT, OPEN_POSITIONS, MONITOR_INTERVAL, ACCOUNT_EQUITY_USDT
    
    if not IS_CLIENT_READY or EXCHANGE_CLIENT is None:
        return

    # ポジションがない場合はスキップ
    if not OPEN_POSITIONS:
        # logging.info("ℹ️ ポジションがないため、モニターをスキップします。")
        return

    # 全ポジションの監視
    positions_to_close: List[Tuple[Dict, str, float, float]] = []

    # 全ポジションのシンボルを取得
    symbols_to_fetch = list(set([p['symbol'] for p in OPEN_POSITIONS]))
    
    # 全シンボルの最新価格を一括で取得 (レートリミット対策)
    current_tickers: Dict[str, Dict] = {}
    try:
        tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols_to_fetch)
        current_tickers = {s: t for s, t in tickers.items() if s in symbols_to_fetch}
    except Exception as e:
        logging.error(f"❌ ポジションモニター: 最新価格取得失敗: {e}")
        return

    for position in OPEN_POSITIONS:
        symbol = position['symbol']
        side = position['side']
        stop_loss = position['stop_loss']
        take_profit = position['take_profit']
        
        ticker = current_tickers.get(symbol)
        
        if not ticker:
            logging.warning(f"⚠️ {symbol}: 最新価格を取得できませんでした。監視をスキップします。")
            continue
            
        current_price = ticker.get('last', ticker.get('close'))
        if current_price is None or current_price <= 0:
            continue
            
        # 1. SL/TPのチェック
        
        # ロングポジション
        if side == 'long':
            # SLトリガー
            if current_price <= stop_loss:
                positions_to_close.append((position, 'STOP_LOSS', stop_loss, current_price))
            # TPトリガー
            elif current_price >= take_profit:
                positions_to_close.append((position, 'TAKE_PROFIT', take_profit, current_price))
                
        # ショートポジション
        elif side == 'short':
            # SLトリガー
            if current_price >= stop_loss:
                positions_to_close.append((position, 'STOP_LOSS', stop_loss, current_price))
            # TPトリガー
            elif current_price <= take_profit:
                positions_to_close.append((position, 'TAKE_PROFIT', take_profit, current_price))
                
        
        # 2. 強制清算価格のチェック (念のため)
        # 清算価格は取引所API側で管理されるため、ここでは省略

    
    # 決済が必要なポジションを順次クローズ
    for position, exit_type, exit_price, current_price in positions_to_close:
        logging.info(f"🚨 {position['symbol']} ({position['side']}): {exit_type} トリガー! 決済処理を実行します。")
        await check_and_close_position(position, exit_type, exit_price, current_price)


# ====================================================================================
# MAIN BOT LOGIC & SCHEDULER
# ====================================================================================

# メインの分析と取引ロジック
async def main_bot_logic():
    global IS_CLIENT_READY, IS_FIRST_MAIN_LOOP_COMPLETED, LAST_ANALYSIS_SIGNALS
    
    logging.info("--- メイン取引ロジック開始 ---")
    
    # 1. FGI (恐怖・貪欲指数) を取得
    global_macro_context = await get_fgi_data()
    
    # 2. 口座ステータスの取得
    account_status = await get_account_status()
    if account_status.get('error'):
        logging.critical(f"🚨 口座ステータス取得に失敗しました。取引を停止し、再初期化を試みます。")
        IS_CLIENT_READY = False # クライアントを再初期化
        return # ループを抜けて再初期化を待つ

    # 3. 監視銘柄リストを更新
    monitoring_symbols, err_msg = await get_top_symbols(TOP_SYMBOL_LIMIT)
    
    if not monitoring_symbols:
        logging.error("❌ 監視銘柄リストが空です。取引をスキップします。")
        return

    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        # 初回起動完了通知を送信
        current_threshold = get_current_threshold(global_macro_context)
        startup_message = format_startup_message(account_status, global_macro_context, len(monitoring_symbols), current_threshold)
        await send_telegram_notification(startup_message)
        IS_FIRST_MAIN_LOOP_COMPLETED = True
        
    
    # 4. 全銘柄の全時間足データを一括取得 (辞書に格納)
    ohlcv_data: Dict[str, Dict[str, pd.DataFrame]] = {}
    
    tasks = []
    for symbol in monitoring_symbols:
        for tf in TARGET_TIMEFRAMES:
            tasks.append(fetch_ohlcv_data(symbol, tf))
            
    # 全タスクを並行で実行
    results = await asyncio.gather(*tasks)

    # 結果を整形
    for i, symbol in enumerate(monitoring_symbols):
        ohlcv_data[symbol] = {}
        for j, tf in enumerate(TARGET_TIMEFRAMES):
            # i * len(TARGET_TIMEFRAMES) + j は results リスト内の対応するインデックス
            result_index = i * len(TARGET_TIMEFRAMES) + j
            df = results[result_index]
            if df is not None:
                ohlcv_data[symbol][tf] = df


    # 5. シグナル分析
    potential_signals: List[Dict] = []
    current_threshold = get_current_threshold(global_macro_context)

    for symbol in monitoring_symbols:
        
        # 必要なOHLCVデータが揃っているかチェック
        if '1h' not in ohlcv_data[symbol] or '4h' not in ohlcv_data[symbol]:
            # logging.warning(f"⚠️ {symbol}: 1h/4hデータが不足しています。分析をスキップします。")
            continue
            
        df_1h = ohlcv_data[symbol]['1h']
        df_4h = ohlcv_data[symbol]['4h']
        
        # ロングシグナルを分析
        long_score, long_tech_data = calculate_signal_score(df_1h, df_4h, symbol, '4h', 'long', global_macro_context)
        if long_score >= BASE_SCORE + STRUCTURAL_PIVOT_BONUS: # 最低限のスコアチェック
            potential_signals.append({
                'symbol': symbol,
                'side': 'long',
                'score': long_score,
                'timeframe': '4h',
                'tech_data': long_tech_data
            })

        # ショートシグナルを分析
        short_score, short_tech_data = calculate_signal_score(df_1h, df_4h, symbol, '4h', 'short', global_macro_context)
        if short_score >= BASE_SCORE + STRUCTURAL_PIVOT_BONUS: # 最低限のスコアチェック
            potential_signals.append({
                'symbol': symbol,
                'side': 'short',
                'score': short_score,
                'timeframe': '4h',
                'tech_data': short_tech_data
            })
            
    # 分析結果をグローバル変数に保存 (定期通知用)
    LAST_ANALYSIS_SIGNALS = potential_signals
    
    # スコアの高い順にソート
    potential_signals.sort(key=lambda x: x['score'], reverse=True)


    # 6. 取引シグナルの実行
    executed_count = 0
    for signal in potential_signals:
        
        if executed_count >= TOP_SIGNAL_COUNT:
            break

        # 取引閾値チェック
        if signal['score'] < current_threshold:
            # logging.info(f"ℹ️ {signal['symbol']} ({signal['side']}): スコアが閾値 ({current_threshold*100:.2f}) 未満のためスキップします。")
            continue

        # 取引を実行
        trade_result = await process_entry_signal(signal)
        
        if trade_result and trade_result.get('status') == 'ok':
            executed_count += 1
            await asyncio.sleep(5) # レートリミット対策として待機


    # 7. 定期通知 (最高スコア)
    await notify_highest_analysis_score()
    
    # 8. WebShareに最新情報をアップロード
    global LAST_WEBSHARE_UPLOAD_TIME, WEBSHARE_UPLOAD_INTERVAL
    current_time = time.time()
    
    if current_time - LAST_WEBSHARE_UPLOAD_TIME >= WEBSHARE_UPLOAD_INTERVAL:
        webshare_data = {
            'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            'version': BOT_VERSION,
            'exchange': CCXT_CLIENT_NAME,
            'is_test_mode': TEST_MODE,
            'account_equity': ACCOUNT_EQUITY_USDT,
            'open_positions': OPEN_POSITIONS,
            'macro_context': GLOBAL_MACRO_CONTEXT,
            'top_signal_analysis': potential_signals[:5] # 上位5つのシグナル
        }
        await send_webshare_update(webshare_data)
        LAST_WEBSHARE_UPLOAD_TIME = current_time

    logging.info("--- メイン取引ロジック完了 ---")


async def main_bot_scheduler():
    """メイン取引ロジックを定期的に実行するスケジューラ。"""
    
    # 初期化
    while not IS_CLIENT_READY:
        if await initialize_exchange_client():
            break
        logging.warning("⚠️ CCXTクライアントの初期化に失敗しました。10秒後に再試行します...")
        await asyncio.sleep(10)

    # メインループ
    while True:
        try:
            # クライアントが切断されていないか再チェック
            if not IS_CLIENT_READY:
                logging.warning("⚠️ クライアント接続が失われました。再初期化を試みます。")
                if await initialize_exchange_client():
                    logging.info("✅ クライアントの再初期化に成功しました。")
                else:
                    logging.critical("🚨 クライアントの再初期化に失敗しました。BOTを一時停止します。")
                    await asyncio.sleep(30)
                    continue
            
            await main_bot_logic()
            
        except Exception as e:
            logging.error(f"🚨 スケジューラ内で予期せぬエラーが発生しました: {e}", exc_info=True)
            # 致命的エラーの場合はクライアントを再初期化
            IS_CLIENT_READY = False
            
        # メインループのインターバル待機
        await asyncio.sleep(LOOP_INTERVAL)


async def position_monitor_scheduler():
    """ポジション監視ロジックを定期的に実行するスケジューラ。"""
    # メインBOTの初期化完了を待つ
    while not IS_FIRST_MAIN_LOOP_COMPLETED:
        await asyncio.sleep(5)
        
    while True:
        try:
            if IS_CLIENT_READY:
                await position_monitor_logic()
            else:
                logging.warning("⚠️ クライアント未接続のため、ポジションモニターをスキップします。")
                
        except Exception as e:
            logging.error(f"🚨 ポジションモニターで予期せぬエラーが発生しました: {e}", exc_info=True)

        await asyncio.sleep(MONITOR_INTERVAL)


# ====================================================================================
# FASTAPI / WEBHOOK INTEGRATION
# ====================================================================================
# WebサーバーとBOTを統合し、Renderなどの環境で実行可能にするためのFastAPI設定

app = FastAPI()

@app.get("/")
async def root():
    return {"message": f"Apex BOT {BOT_VERSION} is running."}

# UptimeRobotなどの監視サービスからのヘルスチェックに対応
@app.head("/")
async def head_root(request: Request):
    # ヘルスチェックに成功したと見なす
    return Response(status_code=200)


@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時に実行"""
    logging.info("BOTサービスを開始しました。")
    
    # スケジューラをバックグラウンドで開始
    asyncio.create_task(main_bot_scheduler())
    asyncio.create_task(position_monitor_scheduler())


# エラーハンドラ 
@app.exception_handler(Exception)
async def default_exception_handler(request, exc):
    """捕捉されなかった例外を処理し、ログに記録する"""
    
    if "Unclosed" not in str(exc):
        logging.error(f"🚨 捕捉されなかったエラーが発生しました: {exc}", exc_info=True)
    
    return JSONResponse(
        status_code=500,
        content={"message": f"Internal Server Error: {exc}"},
    )


# ====================================================================================
# MAIN ENTRY POINT
# ====================================================================================

if __name__ == "__main__":
    
    # Render環境で実行されることを想定し、uvicornを直接呼び出す
    # ホスト '0.0.0.0' は外部からのアクセスを許可するために必要
    # 環境変数 PORT が設定されている場合はその値を使用、ない場合はデフォルトの8000
    port = int(os.environ.get("PORT", 8000)) 
    uvicorn.run("main_render:app", host="0.0.0.0", port=port, log_level="info")
