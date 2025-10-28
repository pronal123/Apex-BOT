# ====================================================================================
# Apex BOT v20.0.44 - Future Trading / 30x Leverage 
# (Feature: 実践的スコアリングロジック、ATR動的リスク管理導入)
# 
# 🚨 致命的エラー修正強化: 
# 1. 💡 修正 (v20.0.44): fetch_tickersのAttributeError ('NoneType' object has no attribute 'keys') 対策を強化
# 2. 💡 修正 (v20.0.44): OBV分析のValueError (The truth value of an array with more than one element is ambiguous) 対策を強化
# 3. 💡 修正 (v20.0.43): np.polyfitの戻り値エラー (ValueError: not enough values to unpack) を修正済み
# 4. 注文失敗エラー (Amount can not be less than zero) 対策
# 5. 通知メッセージでEntry/SL/TP/清算価格が0になる問題を解決 (v20.0.42で対応済み)
# 6. 新規: ダミーロジックを実践的スコアリングロジックに置換 (v20.0.43)
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
ACCOUNT_EQUITY_USDT: float = 0.0 

if TEST_MODE:
    logging.warning("⚠️ WARNING: TEST_MODE is active. Trading is disabled.")

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
            
            # set_leverage() が openType と positionType の両方を要求するため、両方の設定を行います。
            for symbol in symbols_to_set_leverage:
                
                # openType: 2 は Cross Margin
                # positionType: 1 は Long (買い) ポジション用
                try:
                    await EXCHANGE_CLIENT.set_leverage(
                        LEVERAGE, 
                        symbol, 
                        params={'openType': 2, 'positionType': 1}
                    )
                    logging.info(f"✅ {symbol} のレバレッジを {LEVERAGE}x (Cross Margin / Long) に設定しました。")
                except Exception as e:
                    logging.warning(f"⚠️ {symbol} のレバレッジ/マージンモード設定 (Long) に失敗しました: {e}")
                
                # 💥 レートリミット対策として遅延を挿入
                await asyncio.sleep(LEVERAGE_SETTING_DELAY)

                # positionType: 2 は Short (売り) ポジション用
                try:
                    await EXCHANGE_CLIENT.set_leverage(
                        LEVERAGE, 
                        symbol, 
                        params={'openType': 2, 'positionType': 2}
                    )
                    logging.info(f"✅ {symbol} のレバレッジを {LEVERAGE}x (Cross Margin / Short) に設定しました。")
                except Exception as e:
                    logging.warning(f"⚠️ {symbol} のレバレッジ/マージンモード設定 (Short) に失敗しました: {e}")
                
                # 💥 レートリミット対策として遅延を挿入
                await asyncio.sleep(LEVERAGE_SETTING_DELAY)

            logging.info(f"✅ MEXCの主要な先物銘柄 ({len(symbols_to_set_leverage)}件) に対し、レバレッジを {LEVERAGE}x、マージンモードを 'cross' に設定しました。")
        
        logging.info(f"✅ CCXTクライアント ({CCXT_CLIENT_NAME}) を先物取引モードで初期化し、市場情報をロードしました。") 
        IS_CLIENT_READY = True
        return True

    except ccxt.AuthenticationError as e:
        logging.critical(f"❌ CCXT初期化失敗 - 認証エラー: APIキー/シークレットを確認してください。{e}", exc_info=True)
    except ccxt.ExchangeNotAvailable as e:
        logging.critical(f"❌ CCXT初期化失敗 - 取引所接続エラー: サーバーが利用できません。{e}", exc_info=True)
    except ccxt.NetworkError as e:
        logging.critical(f"❌ CCXT初期化失敗 - ネットワークエラー/タイムアウト: 接続を確認してください。{e}", exc_info=True)
    except Exception as e:
        logging.critical(f"❌ CCXTクライアント初期化失敗 - 予期せぬエラー: {e}", exc_info=True)
    
    EXCHANGE_CLIENT = None
    return False

async def fetch_account_status() -> Dict:
    global EXCHANGE_CLIENT, ACCOUNT_EQUITY_USDT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 口座ステータス取得失敗: CCXTクライアントが準備できていません。")
        return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}
        
    try:
        balance = None
        if EXCHANGE_CLIENT.id == 'mexc':
            logging.info("ℹ️ MEXC: fetch_balance(type='swap') を使用して口座情報を取得します。")
            # MEXCはfetch_balance(type='swap')を使用
            balance = await EXCHANGE_CLIENT.fetch_balance(params={'defaultType': 'swap'})
        else:
            fetch_params = {'type': 'future'} if TRADE_TYPE == 'future' else {}
            balance = await EXCHANGE_CLIENT.fetch_balance(params=fetch_params)
            
        if not balance:
            raise Exception("Balance object is empty.")

        total_usdt_balance = balance.get('total', {}).get('USDT', 0.0)

        # MEXC特有のフォールバックロジック (infoからtotalEquityを探す)
        if EXCHANGE_CLIENT.id == 'mexc' and balance.get('info'):
            raw_data = balance['info']
            mexc_raw_data = None
            if isinstance(raw_data, dict) and 'data' in raw_data:
                mexc_raw_data = raw_data.get('data')
            else:
                mexc_raw_data = raw_data
                
            mexc_data: Optional[Dict] = None
            if isinstance(mexc_raw_data, list) and len(mexc_raw_data) > 0:
                if isinstance(mexc_raw_data[0], dict):
                    mexc_data = mexc_raw_data[0]
            elif isinstance(mexc_raw_data, dict):
                 mexc_data = mexc_raw_data
                
            if mexc_data:
                total_usdt_balance_fallback = 0.0
                if mexc_data.get('currency') == 'USDT':
                    total_usdt_balance_fallback = float(mexc_data.get('totalEquity', 0.0))
                elif mexc_data.get('assets') and isinstance(mexc_data['assets'], list):
                    for asset in mexc_data['assets']:
                        if asset.get('asset') == 'USDT':
                            total_usdt_balance_fallback = float(asset.get('totalEquity', 0.0))
                            break
                            
                # どちらか大きい方を使用 (ccxtの計算が間違っている場合に備えて)
                if total_usdt_balance_fallback > total_usdt_balance:
                    total_usdt_balance = total_usdt_balance_fallback
                    logging.warning(f"⚠️ MEXCのフォールバックロジックでUSDT総資産を更新: {format_usdt(total_usdt_balance)}")


        # 総資産をグローバル変数に保存 (リスク管理に使用)
        ACCOUNT_EQUITY_USDT = float(total_usdt_balance)
        logging.info(f"💰 口座総資産 (USDT Equity): {format_usdt(ACCOUNT_EQUITY_USDT)}")

        # オープンポジション情報の取得
        # MEXCはfetch_positionsを使用。Binance/Bybitはfetch_positions_riskを使用
        positions: List[Dict] = []
        if EXCHANGE_CLIENT.has['fetchPositions']:
            positions = await EXCHANGE_CLIENT.fetch_positions()

        open_positions = []
        for p in positions:
            # ポジションサイズが0より大きい、かつUSDT建て先物シンボルのみを抽出
            if (p.get('contracts', 0) != 0 or p.get('info', {}).get('vol', 0) != 0) and \
               p.get('symbol', '').endswith('/USDT') and \
               p.get('type') in ['swap', 'future']:
                
                # 必要な情報を抽出/整形
                entry_price_raw = p.get('entryPrice', 0.0)
                contracts_raw = p.get('contracts', 0.0)
                
                # contractsが取れない場合、MEXC特有の 'vol' フィールドを試す
                if contracts_raw == 0 and EXCHANGE_CLIENT.id == 'mexc':
                    contracts_raw = float(p.get('info', {}).get('vol', 0.0))
                
                # 名目価値 (Notional Value) を計算
                notional_value = abs(contracts_raw * entry_price_raw)
                
                # side (long/short) の判定
                side = 'long' if contracts_raw > 0 else 'short'
                
                # 最終的なポジション情報 (ボットが管理すべき最低限の情報)
                open_positions.append({
                    'symbol': p['symbol'],
                    'side': side,
                    'contracts': contracts_raw,
                    'entry_price': entry_price_raw,
                    'filled_usdt': notional_value, # 名目価値
                    'liquidation_price': p.get('liquidationPrice', 0.0),
                    'timestamp': p.get('timestamp', int(time.time() * 1000)),
                    'stop_loss': 0.0, # ボット内で動的に設定/管理
                    'take_profit': 0.0, # ボット内で動的に設定/管理
                    'leverage': p.get('leverage', LEVERAGE),
                })
        
        # グローバル変数に保存
        global OPEN_POSITIONS
        OPEN_POSITIONS = open_positions
        
        logging.info(f"✅ ポジション情報 ({len(open_positions)}件) を取得しました。")

        return {
            'total_usdt_balance': ACCOUNT_EQUITY_USDT,
            'open_positions': open_positions,
            'error': False
        }

    except Exception as e:
        logging.error(f"❌ 口座情報 (残高/ポジション) 取得中にエラーが発生しました: {e}", exc_info=True)
        return {'total_usdt_balance': ACCOUNT_EQUITY_USDT, 'open_positions': OPEN_POSITIONS, 'error': True, 'error_message': str(e)}


async def fetch_top_volume_symbols() -> List[str]:
    global EXCHANGE_CLIENT, CURRENT_MONITOR_SYMBOLS, TOP_SYMBOL_LIMIT
    
    if SKIP_MARKET_UPDATE:
        logging.warning("⚠️ SKIP_MARKET_UPDATEが有効です。銘柄リストの更新をスキップします。")
        return CURRENT_MONITOR_SYMBOLS
        
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 銘柄リスト取得失敗: CCXTクライアントが準備できていません。")
        return CURRENT_MONITOR_SYMBOLS
        
    try:
        logging.info("⏳ 出来高トップ銘柄のリストを更新中...")
        
        # 出来高トップ銘柄リストを取得 (MEXCはfetch_tickers()に依存)
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        # 💡 【FIX 1】: tickersがNoneまたは辞書型でない場合のチェックを追加
        if not isinstance(tickers, dict) or tickers is None:
            logging.error(f"❌ 出来高トップ銘柄の取得中にエラーが発生しました: fetch_tickersが有効なデータを返しませんでした (Type: {type(tickers)})。'NoneType' object has no attribute 'keys'を回避。")
            return CURRENT_MONITOR_SYMBOLS # 既存のリストを返す
            
        marketIds = list(tickers.keys())

        # USDT建て先物/Swap市場のみにフィルタリング
        usdt_future_tickers: List[Dict] = []
        for symbol, ticker_info in tickers.items():
            # CCXTのシンボル形式 (例: BTC/USDT:USDT)
            market = EXCHANGE_CLIENT.markets.get(symbol)
            
            # USDT建て、先物/Swap、アクティブな市場のみを対象とする
            if market and market.get('quote') == 'USDT' and \
               market.get('type') in ['swap', 'future'] and \
               market.get('active'):
                
                # 出来高 (volume) を取得
                volume_usdt_base = ticker_info.get('baseVolume', 0.0) # 基本通貨での出来高
                volume_usdt_quote = ticker_info.get('quoteVolume', 0.0) # クォート通貨 (USDT) での出来高
                
                # 出来高が大きい方を採用
                volume_usdt = max(volume_usdt_base * ticker_info.get('last', 1.0), volume_usdt_quote)

                # CCXT標準のシンボル形式 (例: BTC/USDT) を使用
                standard_symbol = market.get('id', symbol) 
                
                usdt_future_tickers.append({
                    'symbol': standard_symbol,
                    'volume_usdt': volume_usdt,
                    'info': ticker_info
                })
                
        # 出来高でソート (降順)
        usdt_future_tickers.sort(key=lambda x: x['volume_usdt'], reverse=True)
        
        # トップN銘柄を選定
        new_symbols = [t['symbol'] for t in usdt_future_tickers if t['symbol'] not in DEFAULT_SYMBOLS][:TOP_SYMBOL_LIMIT]
        
        # 基本監視銘柄と統合 (重複を排除し、最大サイズを制限)
        combined_symbols = list(set(DEFAULT_SYMBOLS + new_symbols))

        # グローバル変数を更新
        CURRENT_MONITOR_SYMBOLS = combined_symbols
        logging.info(f"✅ 出来高トップ銘柄のリストを更新完了。監視銘柄数: {len(CURRENT_MONITOR_SYMBOLS)}件。")
        return CURRENT_MONITOR_SYMBOLS

    except Exception as e:
        logging.error(f"❌ 出来高トップ銘柄の取得中にエラーが発生しました: {e}", exc_info=True)
        # エラー発生時は、前回成功したリスト (またはデフォルトリスト) を返す
        return CURRENT_MONITOR_SYMBOLS


async def fetch_fgi_data() -> None:
    """FGI (恐怖・貪欲指数) を取得し、グローバルマクロコンテキストを更新する。"""
    global GLOBAL_MACRO_CONTEXT, FGI_PROXY_BONUS_MAX
    
    logging.info("⏳ FGI (恐怖・貪欲指数) を取得中...")
    
    try:
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        if data and 'data' in data and len(data['data']) > 0:
            fgi_raw = data['data'][0]
            value = int(fgi_raw.get('value', 50))
            value_classification = fgi_raw.get('value_classification', 'Neutral')
            
            # FGI Proxyの計算: [-1.0 (Extreme Fear) から +1.0 (Extreme Greed)]
            # 0-100を-1から1に変換: (FGI / 50) - 1
            fgi_proxy = (value / 50.0) - 1.0
            
            # マクロボーナス/ペナルティ: FGI Proxyが正の時にLongにボーナス、負の時にShortにボーナスを付与
            # 最大ボーナスをFGI_PROXY_BONUS_MAXに制限
            fgi_bonus_value = fgi_proxy * FGI_PROXY_BONUS_MAX
            
            GLOBAL_MACRO_CONTEXT['fgi_proxy'] = fgi_proxy
            GLOBAL_MACRO_CONTEXT['fgi_raw_value'] = f"{value} ({value_classification})"
            GLOBAL_MACRO_CONTEXT['fgi_bonus_value'] = fgi_bonus_value
            
            logging.info(f"✅ FGIデータ取得完了: {value} ({value_classification}) -> Proxy: {fgi_proxy:+.4f}")
            
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ FGIデータ取得エラー: {e}")
        # 失敗時は前回の値を維持するか、デフォルト値 (中立) に戻す
        GLOBAL_MACRO_CONTEXT['fgi_proxy'] = 0.0
        GLOBAL_MACRO_CONTEXT['fgi_raw_value'] = 'N/A'
        GLOBAL_MACRO_CONTEXT['fgi_bonus_value'] = 0.0


async def fetch_ohlcv_data(symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
    """指定された銘柄と時間足のOHLCVデータを取得する。"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return None
        
    try:
        limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 500)
        
        # CCXTのシンボルを市場情報から取得 (例: BTC/USDT -> BTC/USDT:USDT)
        ccxt_symbol = EXCHANGE_CLIENT.markets[symbol]['symbol']
        
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(ccxt_symbol, timeframe=timeframe, limit=limit)
        
        if not ohlcv or len(ohlcv) < limit:
            logging.warning(f"⚠️ {symbol} {timeframe}: データ数が不足しています (取得数: {len(ohlcv)} / 必要数: {limit})。")
            # データ数が少ない場合は分析をスキップさせるためNoneを返す
            return None

        # DataFrameに変換
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('datetime', inplace=True)
        
        return df
        
    except KeyError:
        logging.warning(f"⚠️ {symbol}: 市場情報に存在しないため、OHLCV取得をスキップします。")
    except Exception as e:
        logging.error(f"❌ {symbol} {timeframe}: OHLCVデータ取得中に予期せぬエラーが発生しました: {e}", exc_info=False)
        
    return None

# ====================================================================================
# CORE TRADING LOGIC - SIGNAL SCORING & ANALYSIS
# ====================================================================================

def calculate_signal_score(symbol: str, ohlcv_data: Dict[str, pd.DataFrame], macro_context: Dict) -> List[Dict]:
    """
    複数時間足のデータに基づき、ロング・ショート両方のシグナルスコアを計算する。
    """
    current_time = datetime.now().timestamp()
    signals: List[Dict] = []
    
    # 💡 ベーススコアとマクロ影響を計算
    fgi_bonus = macro_context.get('fgi_bonus_value', 0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    
    # 流動性ボーナス（TOP銘柄リストに含まれていれば付与）
    liquidity_bonus_value = 0.0
    if symbol in CURRENT_MONITOR_SYMBOLS[:10]: # TOP 10
        liquidity_bonus_value = LIQUIDITY_BONUS_MAX * 1.0
    elif symbol in CURRENT_MONITOR_SYMBOLS[:20]: # TOP 20
        liquidity_bonus_value = LIQUIDITY_BONUS_MAX * 0.7
    elif symbol in CURRENT_MONITOR_SYMBOLS[:40]: # TOP 40
        liquidity_bonus_value = LIQUIDITY_BONUS_MAX * 0.3
    
    # ロングとショートのシグナルを並行して計算
    for side in ['long', 'short']:
        
        total_score = BASE_SCORE + STRUCTURAL_PIVOT_BONUS # 基礎点
        tech_data: Dict[str, Any] = {
            'timeframe_scores': {}, 
            'long_term_reversal_penalty_value': 0.0,
            'macd_penalty_value': 0.0,
            'rsi_momentum_bonus_value': 0.0,
            'volatility_penalty_value': 0.0,
            'obv_momentum_bonus_value': 0.0,
            'liquidity_bonus_value': liquidity_bonus_value,
            'sentiment_fgi_proxy_bonus': fgi_bonus + forex_bonus, 
            'structural_pivot_bonus': STRUCTURAL_PIVOT_BONUS
        }
        
        # 銘柄と時間足ごとに分析
        for timeframe, ohlcv in ohlcv_data.items():
            
            if ohlcv is None:
                continue

            # ----------------------------------------------------
            # 1. ボラティリティ分析 (BB幅による過熱ペナルティ)
            # ----------------------------------------------------
            try:
                # Bollinger Bands (BB)
                bbands = ohlcv.ta.bbands(close='close', length=20, std=2.0, append=False)
                
                if bbands.empty or 'BBU_20_2.0' not in bbands.columns:
                     raise KeyError("'BBU_20_2.0' is missing or BB calculation failed.")
                
                last_row = bbands.iloc[-1]
                close_price = ohlcv['close'].iloc[-1]
                
                bbu = last_row['BBU_20_2.0']
                bbl = last_row['BBL_20_2.0']
                
                bb_width_rate = (bbu - bbl) / close_price if close_price > 0 else 0.0
                
                # BB幅が価格のN%を超えている場合は過熱とみなし、ペナルティを付与
                if bb_width_rate > VOLATILITY_BB_PENALTY_THRESHOLD:
                    # 閾値を超えた分に応じてペナルティを課す (最大ペナルティは -0.05 程度を想定)
                    penalty = -min(0.05, (bb_width_rate - VOLATILITY_BB_PENALTY_THRESHOLD) * 2.0)
                    tech_data['volatility_penalty_value'] += penalty
                    total_score += penalty

            except Exception as e:
                logging.error(f"❌ {symbol}: ボラティリティ分析エラー: {e}", exc_info=False)
                pass # エラーが発生しても続行

            # ----------------------------------------------------
            # 2. 長期トレンド一致/逆行 (200期間SMA)
            # ----------------------------------------------------
            try:
                if timeframe == '4h' or timeframe == '1h': # 長めの時間足で実行
                    sma_200 = ohlcv['close'].iloc[-LONG_TERM_SMA_LENGTH:].mean()
                    current_price = ohlcv['close'].iloc[-1]
                    
                    is_uptrend = current_price > sma_200
                    
                    if side == 'long':
                        if is_uptrend:
                            total_score += LONG_TERM_REVERSAL_PENALTY # 順張りボーナス
                            tech_data['long_term_reversal_penalty_value'] += LONG_TERM_REVERSAL_PENALTY
                        else:
                            total_score -= LONG_TERM_REVERSAL_PENALTY # 逆張りペナルティ
                            tech_data['long_term_reversal_penalty_value'] -= LONG_TERM_REVERSAL_PENALTY
                    elif side == 'short':
                        if not is_uptrend:
                            total_score += LONG_TERM_REVERSAL_PENALTY # 順張りボーナス
                            tech_data['long_term_reversal_penalty_value'] += LONG_TERM_REVERSAL_PENALTY
                        else:
                            total_score -= LONG_TERM_REVERSAL_PENALTY # 逆張りペナルティ
                            tech_data['long_term_reversal_penalty_value'] -= LONG_TERM_REVERSAL_PENALTY
            except Exception as e:
                logging.error(f"❌ {symbol}: 長期トレンド分析エラー: {e}", exc_info=False)
                pass

            # ----------------------------------------------------
            # 3. MACDモメンタム一致 (MACDとSignalラインのクロス方向)
            # ----------------------------------------------------
            try:
                macd_data = ohlcv.ta.macd(append=False)
                
                # 最新のMACDとSignalライン
                macd_line = macd_data['MACD_12_26_9'].iloc[-1]
                signal_line = macd_data['MACDH_12_26_9'].iloc[-1] # MACDHはMACD - Signal

                # MACDラインがSignalラインの上にあるか (MACDH > 0)
                is_bullish_cross = signal_line > 0
                
                if side == 'long':
                    if is_bullish_cross:
                        total_score += MACD_CROSS_PENALTY
                        tech_data['macd_penalty_value'] += MACD_CROSS_PENALTY
                    else:
                        total_score -= MACD_CROSS_PENALTY
                        tech_data['macd_penalty_value'] -= MACD_CROSS_PENALTY
                elif side == 'short':
                    if not is_bullish_cross:
                        total_score += MACD_CROSS_PENALTY
                        tech_data['macd_penalty_value'] += MACD_CROSS_PENALTY
                    else:
                        total_score -= MACD_CROSS_PENALTY
                        tech_data['macd_penalty_value'] -= MACD_CROSS_PENALTY
            except Exception as e:
                logging.error(f"❌ {symbol}: MACD分析エラー: {e}", exc_info=False)
                pass

            # ----------------------------------------------------
            # 4. RSIモメンタム加速/適正水準 (RSI 40-60)
            # ----------------------------------------------------
            try:
                rsi = ohlcv.ta.rsi(length=14, append=False).iloc[-1]
                
                rsi_bonus = 0.0
                if side == 'long':
                    # ロングシグナル: 40-60のレンジ内、または40付近からの上昇を評価
                    if RSI_MOMENTUM_LOW <= rsi <= 60:
                        rsi_bonus = RSI_DIVERGENCE_BONUS * (1 - abs(50 - rsi) / 10) # 50に近いほど高得点
                    elif 30 < rsi < RSI_MOMENTUM_LOW: # 40を下回っていても、売られすぎ手前なら微調整
                        rsi_bonus = RSI_DIVERGENCE_BONUS * 0.2
                        
                elif side == 'short':
                    # ショートシグナル: 40-60のレンジ内、または60付近からの下落を評価
                    if 40 <= rsi <= (100 - RSI_MOMENTUM_LOW):
                        rsi_bonus = RSI_DIVERGENCE_BONUS * (1 - abs(50 - rsi) / 10) # 50に近いほど高得点
                    elif (100 - 30) > rsi > (100 - RSI_MOMENTUM_LOW): # 60を上回っていても、買われすぎ手前なら微調整
                        rsi_bonus = RSI_DIVERGENCE_BONUS * 0.2
                        
                total_score += rsi_bonus
                tech_data['rsi_momentum_bonus_value'] += rsi_bonus

            except Exception as e:
                logging.error(f"❌ {symbol}: RSI分析エラー: {e}", exc_info=False)
                pass
                
            # ----------------------------------------------------
            # 5. OBV出来高確証 (出来高トレンドと価格トレンドの一致)
            # ----------------------------------------------------
            try:
                # OBV (On Balance Volume) を計算
                ohlcv['OBV'] = ta.obv(ohlcv['close'], ohlcv['volume'])
                
                # OBVトレンド分析 (傾きを検出)
                
                # 30期間の線形回帰 (np.polyfit) を実行
                obv_df_short = ohlcv['OBV'].iloc[-30:]
                
                # 💡 修正2.1: データフレームが空でないか、またはNaNしかない場合をチェック
                if obv_df_short.dropna().empty:
                    logging.warning(f"⚠️ {symbol}: OBV分析スキップ: データが空またはすべてNaNです。")
                    obv_momentum_bonus_value = 0.0
                else:
                    # np.polyfitの入力が問題ないことを確認
                    x = np.arange(len(obv_df_short))
                    y = obv_df_short.values
                    
                    # NaNを取り除く (両方の配列から同じインデックスを削除)
                    valid_indices = ~np.isnan(y)
                    x_valid = x[valid_indices]
                    y_valid = y[valid_indices]
                    
                    if len(y_valid) < 2:
                        logging.warning(f"⚠️ {symbol}: OBV分析スキップ: NaN除去後、有効データが2点未満です。")
                        obv_momentum_bonus_value = 0.0
                    else:
                        # 💡 np.polyfitの戻り値はタプルで、`slope`と`intercept`にアンパックされる
                        slope, intercept = np.polyfit(x_valid, y_valid, 1)
                    
                        # 💡 【FIX 2】: slopeがNumPy配列やリストになっていた場合にスカラー値を取り出す
                        actual_slope: float
                        if isinstance(slope, (np.ndarray, list)) and len(slope) > 0:
                             # 配列だった場合、先頭要素を傾きとして使用
                             actual_slope = float(slope[0])
                        else:
                             # 通常のfloat/scalarの場合
                             actual_slope = float(slope)
                            
                        
                        # 傾きがゼロではないかチェック
                        if abs(actual_slope) > 0.0001: 
                            is_obv_up = actual_slope > 0
                            
                            # OBVトレンドとシグナル方向の一致を確認
                            if (side == 'long' and is_obv_up) or (side == 'short' and not is_obv_up):
                                # 出来高が価格トレンドを確証している
                                total_score += OBV_MOMENTUM_BONUS
                                tech_data['obv_momentum_bonus_value'] += OBV_MOMENTUM_BONUS
                        
            except Exception as e:
                logging.error(f"❌ {symbol}: OBV分析エラー: {e}", exc_info=True)
                pass


        # ----------------------------------------------------
        # 6. グローバルマクロ影響と流動性ボーナスを追加
        # ----------------------------------------------------
        
        # FGIと為替のボーナス/ペナルティを適用
        # ロングの場合: FGI Proxyが正ならボーナス、負ならペナルティ
        # ショートの場合: FGI Proxyが負ならボーナス、正ならペナルティ
        macro_score_impact = 0.0
        if side == 'long':
            macro_score_impact = fgi_bonus + forex_bonus
        elif side == 'short':
            # ショートシグナルの場合、FGIの方向を反転させる
            macro_score_impact = -(fgi_bonus + forex_bonus)
            
        total_score += macro_score_impact
        tech_data['sentiment_fgi_proxy_bonus'] = macro_score_impact # 最終的な影響値を格納
        
        # 流動性ボーナスを追加
        total_score += liquidity_bonus_value
        
        # 最終スコアを0.0から1.0の間にクランプ
        final_score = max(0.0, min(1.0, total_score))
        
        signals.append({
            'symbol': symbol,
            'side': side,
            'score': final_score,
            'timeframe': TARGET_TIMEFRAMES, # 複合的な分析結果
            'tech_data': tech_data,
            'timestamp': current_time,
            'rr_ratio': RR_RATIO_TARGET # 初期値 (ATR計算後に更新される)
        })

    return signals

def calculate_atr_based_positions(signal: Dict, ohlcv: pd.DataFrame) -> Dict:
    """
    ATRに基づき、SL/TP/契約数/RR比率を計算し、シグナル辞書を更新する。
    """
    if ohlcv is None or ohlcv.empty:
        logging.error(f"❌ {signal['symbol']} ATR計算失敗: OHLCVデータが空です。")
        return {**signal, 'entry_price': 0.0, 'stop_loss': 0.0, 'take_profit': 0.0, 'liquidation_price': 0.0}

    try:
        current_price = ohlcv['close'].iloc[-1]
        
        # ATRを計算
        ohlcv['ATR'] = ta.atr(ohlcv['high'], ohlcv['low'], ohlcv['close'], length=ATR_LENGTH)
        atr_value = ohlcv['ATR'].iloc[-1]

        # 最小リスク幅を価格に基づいて計算 (例: 0.8%)
        min_risk_amount = current_price * MIN_RISK_PERCENT
        
        # ATRに基づくリスク幅 (SL/TPの距離) を決定 (ATR * Multiplier)
        # 💡 ATRリスク幅と最小リスク幅の大きい方を採用することで、ATRが低い時のリスクを担保
        atr_risk_amount = max(atr_value * ATR_MULTIPLIER_SL, min_risk_amount)
        
        entry_price = current_price
        stop_loss = 0.0
        take_profit = 0.0
        
        if signal['side'] == 'long':
            stop_loss = entry_price - atr_risk_amount
            take_profit = entry_price + (atr_risk_amount * RR_RATIO_TARGET)
            
            # SLが極端に浅くなるのを防ぐためのガード
            if stop_loss > entry_price: 
                 stop_loss = entry_price * 0.999 

        elif signal['side'] == 'short':
            stop_loss = entry_price + atr_risk_amount
            take_profit = entry_price - (atr_risk_amount * RR_RATIO_TARGET)
            
            # SLが極端に浅くなるのを防ぐためのガード
            if stop_loss < entry_price:
                 stop_loss = entry_price * 1.001
        
        # SL/TP/リスクリワード比率を更新
        risk_width = abs(entry_price - stop_loss)
        reward_width = abs(take_profit - entry_price)
        rr_ratio_actual = reward_width / risk_width if risk_width > 0 else 0.0
        
        # 清算価格の計算 (推定値)
        liquidation_price = calculate_liquidation_price(
            entry_price=entry_price, 
            leverage=LEVERAGE, 
            side=signal['side'], 
            maintenance_margin_rate=MIN_MAINTENANCE_MARGIN_RATE
        )
        
        # 最終的なシグナル辞書を構築/更新
        updated_signal = {
            **signal,
            'entry_price': entry_price,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'liquidation_price': liquidation_price,
            'rr_ratio': rr_ratio_actual,
            'atr_risk_amount': atr_risk_amount,
        }
        
        # 名目ロットから取引数量を計算 (トレード実行時に実行)
        # 💡 ここでは数量計算は省略し、トレード実行時に関数の引数として渡す
        
        return updated_signal

    except Exception as e:
        logging.error(f"❌ {signal['symbol']} ATR/ポジション計算中にエラーが発生しました: {e}", exc_info=True)
        return {**signal, 'entry_price': 0.0, 'stop_loss': 0.0, 'take_profit': 0.0, 'liquidation_price': 0.0}
    
# ====================================================================================
# CORE TRADING LOGIC - EXECUTION
# ====================================================================================

async def execute_trade(signal: Dict) -> Dict:
    """
    シグナルに基づいて取引を実行し、SL/TPを設定する。
    """
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    symbol = signal['symbol']
    side = signal['side']
    entry_price = signal.get('entry_price', 0.0)
    stop_loss = signal.get('stop_loss', 0.0)
    take_profit = signal.get('take_profit', 0.0)
    
    trade_result: Dict = {'status': 'error', 'error_message': '初期化エラー', 'filled_usdt': 0.0, 'filled_amount': 0.0}

    if TEST_MODE:
        logging.warning(f"⚠️ TEST_MODE: {symbol} {side.upper()}の取引をスキップします。")
        trade_result = {
            'status': 'ok', 
            'error_message': 'TEST_MODE', 
            'filled_usdt': FIXED_NOTIONAL_USDT, 
            'filled_amount': FIXED_NOTIONAL_USDT / entry_price if entry_price > 0 else 0.0,
            'entry_price': entry_price, 
            'stop_loss': stop_loss,
            'take_profit': take_profit,
        }
        
    elif not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        trade_result['error_message'] = 'CCXTクライアントが準備できていません。'
        logging.error(f"❌ {symbol} {side.upper()} 取引失敗: {trade_result['error_message']}")

    elif entry_price <= 0 or stop_loss <= 0 or take_profit <= 0:
        trade_result['error_message'] = 'エントリー/SL/TP価格が不正です。'
        logging.error(f"❌ {symbol} {side.upper()} 取引失敗: {trade_result['error_message']}")

    else:
        try:
            # 1. 取引数量の計算
            market = EXCHANGE_CLIENT.markets[symbol]
            price_precision = market.get('precision', {}).get('price', 8)
            amount_precision = market.get('precision', {}).get('amount', 4)
            
            # 💡 名目ロット (USDT) から数量を計算
            amount_raw = FIXED_NOTIONAL_USDT / entry_price
            
            # 取引所指定の精度に丸める (ceil: 買いの場合、floor: 売り/ショートの場合)
            if side == 'long':
                # 買いの場合、切り上げ
                amount = math.ceil(amount_raw * (10 ** amount_precision)) / (10 ** amount_precision)
            else:
                # 売り/ショートの場合、切り捨て
                amount = math.floor(amount_raw * (10 ** amount_precision)) / (10 ** amount_precision)
            
            
            # 最小取引サイズ (min_notional) のチェック
            min_notional = market.get('limits', {}).get('cost', {}).get('min', 0.0)
            current_notional = amount * entry_price
            
            if current_notional < min_notional:
                trade_result['error_message'] = f"最小取引サイズ違反: {current_notional:.2f} USDT < {min_notional:.2f} USDT"
                logging.error(f"❌ {symbol} {side.upper()} 取引失敗: {trade_result['error_message']}")
                return trade_result
            
            # 2. 成行注文の実行
            order_params = {'defaultType': TRADE_TYPE}
            
            # MEXCの先物取引では、`params`に`positionSide`が必要な場合がある
            if EXCHANGE_CLIENT.id == 'mexc':
                # 'buy'='long' / 'sell'='short'
                order_params['positionSide'] = 'Long' if side == 'long' else 'Short'

            
            # 成行注文
            order = await EXCHANGE_CLIENT.create_order(
                symbol,
                'market', 
                side, # 'buy' or 'sell' (long or short)
                amount,
                price=None, # Market order
                params=order_params
            )
            
            # 注文結果から約定数量と約定価格を取得
            filled_amount = order.get('filled', amount)
            filled_price = order.get('price', entry_price) # 成行注文では約定価格が入る
            
            if filled_amount > 0 and filled_price > 0:
                trade_result['status'] = 'ok'
                trade_result['filled_amount'] = filled_amount
                trade_result['filled_usdt'] = filled_amount * filled_price
                trade_result['entry_price'] = filled_price # 実際の約定価格
                
                logging.info(f"✅ {symbol} {side.upper()} 成行注文成功: {filled_amount:.4f} @ {format_price(filled_price)}")
                
                # 3. SL/TP注文の実行 (Post-execution logic)
                
                # CCXTのOco/Trailing Stopは複雑なため、ここでは単純なStop/Limit (Take Profit) を使用
                
                # ストップロス注文 (Stop Market Order)
                sl_side = 'sell' if side == 'long' else 'buy'
                
                # 注文のパラメータ調整 (MEXC)
                sl_params = order_params.copy()
                if EXCHANGE_CLIENT.id == 'mexc':
                    # SL/TPはCloseを意図するため、positionSideは通常 'Both' (または指定不要)だが、
                    # MEXCのAPIは複雑なため、ここでは`positionSide`を省略するか、`Close`を意図するパラメータを使用
                    pass 

                try:
                    # ストップマーケット注文 (Stop Market)
                    sl_order = await EXCHANGE_CLIENT.create_order(
                        symbol,
                        'stop_market', # Stop Market order
                        sl_side,
                        filled_amount,
                        price=None, # Stop Marketではトリガー価格のみが必要
                        params={**sl_params, 'stopPrice': stop_loss}
                    )
                    logging.info(f"✅ SL注文 ({sl_side.upper()} @ {format_price(stop_loss)}) を設定しました。")
                except Exception as sl_e:
                    logging.error(f"❌ SL注文設定失敗: {sl_e}")
                    # SL設定失敗は致命的ではないが、Telegramで通知すべき
                    await send_telegram_notification(f"🚨 **SL設定失敗警告**\n{symbol} {side.upper()}ポジションのSL設定に失敗しました: <code>{sl_e}</code>")
                
                
                # テイクプロフィット注文 (Limit Order at TP price)
                tp_side = 'sell' if side == 'long' else 'buy'
                try:
                    # リミット注文 (Take Profit Limit)
                    tp_order = await EXCHANGE_CLIENT.create_order(
                        symbol,
                        'limit', # Limit order
                        tp_side,
                        filled_amount,
                        price=take_profit,
                        params=tp_params
                    )
                    logging.info(f"✅ TP注文 ({tp_side.upper()} @ {format_price(take_profit)}) を設定しました。")
                except Exception as tp_e:
                    logging.error(f"❌ TP注文設定失敗: {tp_e}")
                    # TP設定失敗は許容できる
                    pass

                # 4. グローバルポジションリストの更新 (SL/TP情報付き)
                # execute_tradeは、position_monitor_schedulerの直後に実行されるため、
                # position_monitor_schedulerの次回の実行で、新しいポジションがfetch_account_statusによって検出されるまで、
                # ここで仮のポジション情報をOPEN_POSITIONSに追加する必要がある。
                OPEN_POSITIONS.append({
                    'symbol': symbol,
                    'side': side,
                    'contracts': filled_amount if side == 'long' else -filled_amount, # Longは正、Shortは負
                    'entry_price': filled_price,
                    'filled_usdt': trade_result['filled_usdt'],
                    'stop_loss': stop_loss, # 実際のSL価格を格納
                    'take_profit': take_profit, # 実際のTP価格を格納
                    'leverage': LEVERAGE,
                    'timestamp': int(time.time() * 1000),
                    'liquidation_price': calculate_liquidation_price(filled_price, LEVERAGE, side)
                })


            else:
                trade_result['error_message'] = f"約定数量が0または約定価格が0です (Filled: {filled_amount:.4f} @ {format_price(filled_price)})"
                logging.error(f"❌ {symbol} {side.upper()} 取引失敗: {trade_result['error_message']}")

        except ccxt.ExchangeError as e:
            # 取引所固有のエラー（例：Not enough balance、最小取引サイズ違反など）
            trade_result['error_message'] = f"取引所エラー ({type(e).__name__}): {e}"
            logging.error(f"❌ {symbol} {side.upper()} 取引所エラー: {e}", exc_info=False)
        except Exception as e:
            trade_result['error_message'] = f"予期せぬ取引エラー: {e}"
            logging.error(f"❌ {symbol} {side.upper()} 予期せぬエラー: {e}", exc_info=True)
            
    return trade_result

async def close_position(position: Dict, exit_type: str = 'MANUAL') -> Dict:
    """
    既存のポジションを成行で決済する。
    """
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    symbol = position['symbol']
    side = position['side']
    contracts = abs(position['contracts']) # 数量は絶対値
    entry_price = position['entry_price']
    
    close_side = 'sell' if side == 'long' else 'buy'
    
    trade_result: Dict = {'status': 'error', 'error_message': '初期化エラー', 'pnl_usdt': 0.0, 'pnl_rate': 0.0, 'exit_price': 0.0}

    if TEST_MODE:
        logging.warning(f"⚠️ TEST_MODE: {symbol} ポジション決済 ({exit_type}) をスキップします。")
        trade_result = {
            'status': 'ok', 
            'error_message': 'TEST_MODE',
            'entry_price': entry_price,
            'exit_price': entry_price * (1.01 if exit_type == 'TP' else 0.99), # 仮のTP/SL結果
            'filled_amount': contracts,
            'exit_type': exit_type
        }
    elif not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        trade_result['error_message'] = 'CCXTクライアントが準備できていません。'
        logging.error(f"❌ {symbol} ポジション決済失敗: {trade_result['error_message']}")

    elif contracts <= 0:
        trade_result['error_message'] = '契約数が0または不正です。'
        logging.error(f"❌ {symbol} ポジション決済失敗: {trade_result['error_message']}")

    else:
        try:
            # 1. 決済注文の実行 (成行)
            order_params = {'defaultType': TRADE_TYPE}
            
            # MEXCの先物取引では、`positionSide`と`closePosition`が必要な場合がある
            if EXCHANGE_CLIENT.id == 'mexc':
                # closePosition: True にすることで、ポジションクローズ注文であることを明示
                order_params['closePosition'] = True
                order_params['positionSide'] = 'Long' if side == 'long' else 'Short'

            
            # 成行注文でポジションをクローズ
            order = await EXCHANGE_CLIENT.create_order(
                symbol,
                'market', 
                close_side, # 'sell' (for long) or 'buy' (for short)
                contracts,
                price=None, # Market order
                params=order_params
            )
            
            # 注文結果から約定数量と約定価格を取得
            filled_amount = order.get('filled', contracts)
            exit_price = order.get('price', entry_price) # 成行注文では約定価格が入る
            
            if filled_amount > 0 and exit_price > 0:
                trade_result['status'] = 'ok'
                trade_result['exit_price'] = exit_price
                trade_result['entry_price'] = entry_price
                trade_result['filled_amount'] = filled_amount
                trade_result['exit_type'] = exit_type
                
                # PnLの計算 (簡単な推定)
                pnl_usdt, pnl_rate = calculate_pnl(position, exit_price)
                trade_result['pnl_usdt'] = pnl_usdt
                trade_result['pnl_rate'] = pnl_rate
                
                logging.info(f"✅ {symbol} ポジション決済成功 ({exit_type}): {filled_amount:.4f} @ {format_price(exit_price)} (PnL: {pnl_usdt:+.2f} USDT)")
                
                # 2. グローバルポジションリストの更新 (削除)
                # ポジションを特定し、リストから削除
                OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p['symbol'] != symbol]
                
                # 3. 関連するオープンオーダー（SL/TP）をキャンセル
                await cancel_open_orders_for_symbol(symbol)


            else:
                trade_result['error_message'] = f"約定数量が0または約定価格が0です (Filled: {filled_amount:.4f} @ {format_price(exit_price)})"
                logging.error(f"❌ {symbol} ポジション決済失敗: {trade_result['error_message']}")

        except ccxt.ExchangeError as e:
            # ポジションがないなどの取引所固有のエラー
            trade_result['error_message'] = f"取引所エラー ({type(e).__name__}): {e}"
            logging.error(f"❌ {symbol} ポジション決済失敗: {e}", exc_info=False)
        except Exception as e:
            trade_result['error_message'] = f"予期せぬ決済エラー: {e}"
            logging.error(f"❌ {symbol} ポジション決済中に予期せぬエラー: {e}", exc_info=True)
            
    return trade_result

def calculate_pnl(position: Dict, exit_price: float) -> Tuple[float, float]:
    """PnL (損益) を計算する。"""
    entry_price = position.get('entry_price', 0.0)
    contracts = abs(position.get('contracts', 0.0))
    side = position.get('side', 'long')
    leverage = position.get('leverage', LEVERAGE)
    filled_usdt = position.get('filled_usdt', 0.0) # ポジションの名目価値 (エントリー時点)
    
    if entry_price <= 0 or contracts <= 0 or filled_usdt <= 0:
        return 0.0, 0.0
        
    pnl_ratio: float
    if side == 'long':
        pnl_ratio = (exit_price - entry_price) / entry_price
    else: # short
        pnl_ratio = (entry_price - exit_price) / entry_price
        
    # PnL (USDT): 名目価値 * 価格変動率
    pnl_usdt = filled_usdt * pnl_ratio
    
    # RoE (Rate of Equity/Return on Equity) は、証拠金に対するリターン。
    # 証拠金 = 名目価値 / レバレッジ
    initial_margin = filled_usdt / leverage
    roe_rate = pnl_usdt / initial_margin
    
    return pnl_usdt, roe_rate

async def cancel_open_orders_for_symbol(symbol: str) -> None:
    """指定された銘柄のオープンオーダーを全てキャンセルする。"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return

    try:
        # MEXCの場合、fetch_open_ordersが対応していない可能性も考慮
        if EXCHANGE_CLIENT.has['fetchOpenOrders']:
            orders = await EXCHANGE_CLIENT.fetch_open_orders(symbol)
            if orders:
                logging.info(f"ℹ️ {symbol}: {len(orders)}件のオープンオーダーをキャンセルします。")
                for order in orders:
                    try:
                        await EXCHANGE_CLIENT.cancel_order(order['id'], symbol)
                    except Exception as e:
                        logging.warning(f"⚠️ 注文ID {order['id']} のキャンセルに失敗しました: {e}")
            else:
                logging.info(f"ℹ️ {symbol}: オープンオーダーはありませんでした。")
    except Exception as e:
        logging.warning(f"⚠️ {symbol}: オープンオーダーの取得/キャンセル中にエラーが発生しました: {e}")


# ====================================================================================
# SCHEDULERS & MAIN LOOP
# ====================================================================================

async def run_main_loop():
    """BOTの主要な分析・取引ロジックを含むメインループ。"""
    global LAST_SUCCESS_TIME, LAST_SIGNAL_TIME, LAST_ANALYSIS_SIGNALS, IS_FIRST_MAIN_LOOP_COMPLETED
    
    logging.info("🚀 メイン分析ループを開始します...")
    
    # 状態の更新 (残高、FGI、銘柄リスト)
    account_status = await fetch_account_status()
    await fetch_fgi_data()
    await fetch_top_volume_symbols() # 出来高TOP銘柄を更新
    
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)

    # 監視銘柄 (DEFAULT_SYMBOLS + TOP_VOLUME) を取得
    symbols_to_monitor = CURRENT_MONITOR_SYMBOLS 
    
    # 💡 分析結果の格納用リストをリセット
    LAST_ANALYSIS_SIGNALS = []
    
    # ----------------------------------------------------
    # 1. データ取得と分析の並列実行
    # ----------------------------------------------------
    
    # OHLCVデータの取得タスクを生成 (全ての銘柄 x 全ての時間足)
    data_tasks: List[Tuple[str, str, asyncio.Future]] = []
    
    for symbol in symbols_to_monitor:
        for tf in TARGET_TIMEFRAMES:
            task = asyncio.create_task(fetch_ohlcv_data(symbol, tf))
            data_tasks.append((symbol, tf, task))

    # 全てのデータ取得タスクの完了を待機
    ohlcv_cache: Dict[str, Dict[str, pd.DataFrame]] = {} # symbol -> {timeframe -> df}
    
    for symbol, tf, task in data_tasks:
        try:
            ohlcv_df = await task
            if symbol not in ohlcv_cache:
                ohlcv_cache[symbol] = {}
            if ohlcv_df is not None:
                ohlcv_cache[symbol][tf] = ohlcv_df
                
        except Exception as e:
            logging.error(f"❌ {symbol} {tf}: データ取得タスクで予期せぬエラーが発生しました: {e}", exc_info=False)
            
    logging.info(f"✅ 全銘柄のOHLCVデータ取得完了。分析に進みます。")

    # ----------------------------------------------------
    # 2. シグナルスコアリングの並列実行
    # ----------------------------------------------------
    
    scoring_tasks = []
    for symbol in symbols_to_monitor:
        # 必要なOHLCVデータが揃っているか確認
        # 💡 ここでは全ての時間足のデータが揃っている必要はないが、最低限1hまたは4hが存在することを推奨
        if symbol in ohlcv_cache and len(ohlcv_cache[symbol]) > 0:
            scoring_tasks.append(
                asyncio.create_task(
                    asyncio.to_thread(
                        calculate_signal_score, 
                        symbol, 
                        ohlcv_cache[symbol], 
                        GLOBAL_MACRO_CONTEXT
                    )
                )
            )
        else:
            logging.warning(f"⚠️ {symbol}: 分析に必要なデータが不足しているためスコアリングをスキップします。")
            
    # 全てのスコアリングタスクの完了を待機
    raw_signals: List[Dict] = []
    for task in asyncio.as_completed(scoring_tasks):
        try:
            signals_for_symbol = await task
            raw_signals.extend(signals_for_symbol)
        except Exception as e:
            logging.error(f"❌ スコアリングタスクで予期せぬエラーが発生しました: {e}", exc_info=True)

    
    # ----------------------------------------------------
    # 3. 最適シグナルの選定と処理
    # ----------------------------------------------------

    # スコアでソートし、取引閾値を超えたシグナルを抽出
    trade_signals = []
    for signal in raw_signals:
        LAST_ANALYSIS_SIGNALS.append(signal) # 全ての分析結果をグローバルに保存 (定時通知用)
        
        if signal['score'] >= current_threshold:
            # クールダウン期間のチェック (最終取引時刻が12時間以内ならスキップ)
            symbol_key = f"{signal['symbol']}_{signal['side']}"
            if current_time - LAST_SIGNAL_TIME.get(symbol_key, 0) < TRADE_SIGNAL_COOLDOWN:
                logging.info(f"ℹ️ {signal['symbol']} {signal['side'].upper()}: クールダウン期間中のためスキップします。")
                continue
                
            # ポジションの重複チェック (すでに同銘柄のポジションがある場合はスキップ)
            if any(p['symbol'] == signal['symbol'] for p in OPEN_POSITIONS):
                logging.info(f"ℹ️ {signal['symbol']} {signal['side'].upper()}: 既にポジションが存在するためスキップします。")
                continue
                
            trade_signals.append(signal)

    # スコア順にソート
    trade_signals.sort(key=lambda x: x['score'], reverse=True)
    
    # ----------------------------------------------------
    # 4. ポジション情報とATRに基づくSL/TP計算 (最上位のシグナルのみ)
    # ----------------------------------------------------
    
    if trade_signals and not TEST_MODE:
        # 💡 最もスコアの高いシグナル (TOP_SIGNAL_COUNT 件まで) のみ処理
        for signal in trade_signals[:TOP_SIGNAL_COUNT]:
            symbol = signal['symbol']
            
            # ATR計算に必要な「最も短い」時間足のデータ (例: 1mまたは5m) を取得
            # ATR計算には連続したデータが必要なため、最も確実なデータ（ここでは1mデータ）を使用する
            
            # 💡 現在はすべての時間足データを取得済みなので、'1m'のデータがあれば使用。なければ'5m'など短いものを優先
            ohlcv_for_atr = None
            for tf in ['1m', '5m', '15m']:
                if symbol in ohlcv_cache and tf in ohlcv_cache[symbol]:
                    ohlcv_for_atr = ohlcv_cache[symbol][tf]
                    break
            
            if ohlcv_for_atr is None:
                logging.error(f"❌ {symbol}: ATR計算に必要な短い時間足のデータが不足しているため取引をスキップします。")
                continue
                
            # ATR/SL/TPを計算してシグナルを更新
            final_signal = calculate_atr_based_positions(signal, ohlcv_for_atr)
            
            # SL/TPが有効であることを再確認 (価格が0になっていないか、など)
            if final_signal['entry_price'] > 0 and final_signal['stop_loss'] > 0 and final_signal['take_profit'] > 0:
                
                # 5. 取引実行
                logging.info(f"🔥 **取引実行シグナル**: {symbol} ({final_signal['side'].upper()}) Score: {final_signal['score']:.2f} @ {format_price(final_signal['entry_price'])}")
                
                trade_result = await execute_trade(final_signal)
                
                # 6. 通知とログの記録
                await send_telegram_notification(format_telegram_message(final_signal, "取引シグナル", current_threshold, trade_result))
                log_signal(final_signal, "取引実行", trade_result)
                
                # 最終取引時刻を更新
                symbol_key = f"{symbol}_{final_signal['side']}"
                LAST_SIGNAL_TIME[symbol_key] = current_time
            else:
                 logging.error(f"❌ {symbol} {signal['side'].upper()}: ATR計算後のSL/TP価格が無効です。取引をスキップします。")

    else:
        logging.info("ℹ️ 取引閾値を超えたシグナルは見つかりませんでした。")
        
    # ----------------------------------------------------
    # 7. ループ完了処理
    # ----------------------------------------------------
    
    # 初回起動完了通知
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        # 初回起動時のみスタートアップメッセージを送信
        startup_message = format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold)
        await send_telegram_notification(startup_message)
        IS_FIRST_MAIN_LOOP_COMPLETED = True
        
    # 定期的な分析結果通知
    if IS_FIRST_MAIN_LOOP_COMPLETED:
        await notify_highest_analysis_score()


    LAST_SUCCESS_TIME = current_time
    logging.info("✅ メイン分析ループが完了しました。")

async def main_bot_scheduler():
    """
    メインの分析・取引ループを定期的に実行するスケジューラ。
    """
    await initialize_exchange_client() # クライアントの初期化を試行

    while True:
        try:
            if IS_CLIENT_READY:
                # メインループを実行
                await run_main_loop()
                
            else:
                logging.warning("⚠️ CCXTクライアントが準備できていないため、メインループをスキップします。再初期化を試みます。")
                # クライアントの再初期化を試みる
                await initialize_exchange_client()
                
            await asyncio.sleep(LOOP_INTERVAL)

        except Exception as e:
            logging.critical(f"🔥 メインスケジューラで致命的なエラーが発生しました: {e}", exc_info=True)
            await send_telegram_notification(f"🚨 **致命的BOTクラッシュ**\nメインループでリカバリ不能なエラーが発生しました: <code>{e}</code>")
            # 致命的エラー発生時も、無限ループから抜けない
            await asyncio.sleep(60) # 60秒待機してから再試行

async def position_monitor_scheduler():
    """
    オープンポジションを監視し、SL/TPトリガーをチェックするスケジューラ。
    """
    global OPEN_POSITIONS
    
    logging.info("⏳ ポジション監視スケジューラを開始します...")

    # メインループの初回完了を待機 (ポジション情報が最新であることを確認してから監視を開始)
    while not IS_FIRST_MAIN_LOOP_COMPLETED:
        logging.info("⏳ メインループの初回完了を待機しています...")
        await asyncio.sleep(MONITOR_INTERVAL) 

    while True:
        try:
            # 1. 最新のポジション情報を取得
            account_status = await fetch_account_status()
            current_positions = account_status['open_positions']
            
            # 💡 グローバル変数も更新されているため、これを使用
            
            if not OPEN_POSITIONS:
                logging.info("ℹ️ 監視対象のポジションはありません。")
                await asyncio.sleep(MONITOR_INTERVAL)
                continue
                
            
            # 2. 監視銘柄の最新価格を取得
            symbols_to_check = [p['symbol'] for p in OPEN_POSITIONS]
            price_tasks = []
            for symbol in symbols_to_check:
                if EXCHANGE_CLIENT.has['fetchTicker']:
                    # fetch_tickerは最新価格を最も迅速に取得する方法
                    task = asyncio.create_task(EXCHANGE_CLIENT.fetch_ticker(symbol))
                    price_tasks.append((symbol, task))

            # 全ての価格取得タスクの完了を待機
            current_prices: Dict[str, float] = {}
            for symbol, task in price_tasks:
                try:
                    ticker = await task
                    current_prices[symbol] = ticker['last']
                except Exception as e:
                    logging.warning(f"⚠️ {symbol}: 最新価格の取得に失敗しました: {e}")

            
            # 3. SL/TPチェックと清算リスクの評価
            
            # ⚠️ ここでは、position_monitor_schedulerの開始時にfetch_account_statusが実行され、
            # OPEN_POSITIONSが最新の取引所情報で上書きされていることを前提としています。
            # また、SL/TPの価格は取引実行時にOPEN_POSITIONSに格納されています。
            
            positions_to_close: List[Tuple[Dict, str]] = []

            for position in OPEN_POSITIONS:
                symbol = position['symbol']
                current_price = current_prices.get(symbol)
                
                if current_price is None:
                    continue
                
                stop_loss = position['stop_loss']
                take_profit = position['take_profit']
                liquidation_price = position['liquidation_price']

                if position['side'] == 'long':
                    # ロング: SLトリガー (現在価格 <= SL価格)
                    if current_price <= stop_loss and stop_loss > 0:
                        positions_to_close.append((position, 'SL_TRIGGER'))
                    # ロング: TPトリガー (現在価格 >= TP価格)
                    elif current_price >= take_profit and take_profit > 0:
                        positions_to_close.append((position, 'TP_TRIGGER'))
                    # 清算価格が近い警告 (清算価格と現在価格の差が5%未満)
                    elif liquidation_price > 0 and (current_price - liquidation_price) / current_price < 0.05:
                        logging.warning(f"🚨 {symbol} Long: 清算価格が接近しています! (Liq: {format_price(liquidation_price)})")
                        
                elif position['side'] == 'short':
                    # ショート: SLトリガー (現在価格 >= SL価格)
                    if current_price >= stop_loss and stop_loss > 0:
                        positions_to_close.append((position, 'SL_TRIGGER'))
                    # ショート: TPトリガー (現在価格 <= TP価格)
                    elif current_price <= take_profit and take_profit > 0:
                        positions_to_close.append((position, 'TP_TRIGGER'))
                    # 清算価格が近い警告 (清算価格と現在価格の差が5%未満)
                    elif liquidation_price > 0 and (liquidation_price - current_price) / current_price < 0.05:
                        logging.warning(f"🚨 {symbol} Short: 清算価格が接近しています! (Liq: {format_price(liquidation_price)})")


            # 4. 決済処理の実行
            for position, exit_type in positions_to_close:
                # 決済前に、取引所側で設定したSL/TP注文をキャンセルする必要がある
                await cancel_open_orders_for_symbol(position['symbol'])
                
                # ポジションの決済
                close_result = await close_position(position, exit_type)
                
                # 決済結果を通知
                if close_result['status'] == 'ok':
                    await send_telegram_notification(format_telegram_message(position, "ポジション決済", current_threshold, close_result, exit_type))
                    log_signal(position, "ポジション決済", close_result)
                else:
                    await send_telegram_notification(f"🚨 **決済失敗**\n{position['symbol']} ポジションの決済 ({exit_type}) に失敗しました: <code>{close_result.get('error_message', '詳細不明')}</code>")


            # 5. WebShareへのアップデート (定期的)
            if time.time() - LAST_WEBSHARE_UPLOAD_TIME > WEBSHARE_UPLOAD_INTERVAL:
                webshare_data = {
                    'status': 'Running',
                    'version': BOT_VERSION,
                    'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
                    'account_equity': ACCOUNT_EQUITY_USDT,
                    'open_positions': OPEN_POSITIONS,
                    'macro_context': GLOBAL_MACRO_CONTEXT,
                    'last_success_time': datetime.fromtimestamp(LAST_SUCCESS_TIME, JST).strftime("%Y-%m-%d %H:%M:%S") if LAST_SUCCESS_TIME > 0 else 'N/A',
                }
                await send_webshare_update(webshare_data)
                global LAST_WEBSHARE_UPLOAD_TIME
                LAST_WEBSHARE_UPLOAD_TIME = time.time()


            await asyncio.sleep(MONITOR_INTERVAL)

        except Exception as e:
            logging.critical(f"🔥 ポジション監視スケジューラで致命的なエラーが発生しました: {e}", exc_info=True)
            await send_telegram_notification(f"🚨 **致命的BOTクラッシュ**\n監視ループでリカバリ不能なエラーが発生しました: <code>{e}</code>")
            await asyncio.sleep(60) # 60秒待機してから再試行
            
            
# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

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
        logging.error(f"❌ 未処理の致命的なエラーが発生しました: {exc}")
        # Telegram通知はループ内で実行されるため、ここでは省略
        
    return JSONResponse(
        status_code=500,
        content={"message": "Internal Server Error", "detail": str(exc)},
    )

# 💡 起動コードはDocker/Renderなどの環境で`uvicorn main_render:app`が実行されることを想定

if __name__ == '__main__':
    # 💡 開発環境での手動実行用 (通常はUvicornで起動される)
    # uvicorn.run(app, host="0.0.0.0", port=8000)
    pass
