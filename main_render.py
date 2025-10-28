# ====================================================================================
# Apex BOT v20.0.43 - Future Trading / 30x Leverage 
# (Feature: 実践的スコアリングロジック、ATR動的リスク管理導入)
# 
# 🚨 致命的エラー修正強化: 
# 1. fetch_tickersのAttributeError ('NoneType' object has no attribute 'keys') 対策 
# 2. 注文失敗エラー (Amount can not be less than zero) 対策
# 3. 💡 修正: 通知メッセージでEntry/SL/TP/清算価格が0になる問題を解決 (v20.0.42で対応済み)
# 4. 💡 新規: ダミーロジックを実践的スコアリングロジックに置換 (v20.0.43)
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
BOT_VERSION = "v20.0.43"            # 💡 BOTバージョンを更新 
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
REQUIRED_OHLCV_LIMITS = {'1m': 1000, '5m': 1000, '15m': 1000, '1h': 1000, '4h': 1000} 

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
                    'filled_usdt': notional_value,  # 名目価値
                    'liquidation_price': p.get('liquidationPrice', 0.0),
                    'timestamp': p.get('timestamp', int(time.time() * 1000)),
                    'stop_loss': 0.0,  # ボット内で動的に設定/管理
                    'take_profit': 0.0,  # ボット内で動的に設定/管理
                    'leverage': p.get('leverage', LEVERAGE),
                    'raw_info': p,  # デバッグ用に生の情報を保持
                })

        # グローバル変数 OPEN_POSITIONS を更新
        global OPEN_POSITIONS
        OPEN_POSITIONS = open_positions
        logging.info(f"✅ ポジション情報 ({len(OPEN_POSITIONS)}件) を取得しました。")
        
        return {
            'total_usdt_balance': ACCOUNT_EQUITY_USDT,
            'open_positions': OPEN_POSITIONS,
            'error': False
        }

    except Exception as e:
        logging.critical(f"❌ 口座ステータス取得中の致命的エラー: {e}", exc_info=True)
        return {'total_usdt_balance': ACCOUNT_EQUITY_USDT, 'open_positions': [], 'error': True}


# ====================================================================================
# DATA ACQUISITION & MARKET SCAN
# ====================================================================================

async def fetch_ohlcv_data(symbol: str, timeframe: str, limit: int = 1000) -> Optional[pd.DataFrame]:
    """指定されたシンボルと時間枠のOHLCVデータを取得する"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return None
        
    try:
        # ccxtのfetch_ohlcvはタイムアウトしやすいので、リトライロジックを追加
        ohlcv = None
        for attempt in range(3):
            try:
                ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
                break
            except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
                logging.warning(f"⚠️ {symbol} {timeframe} OHLCV取得リトライ: タイムアウト/ネットワークエラー ({e})")
                await asyncio.sleep(2 ** attempt) # 指数バックオフ
            except ccxt.ExchangeError as e:
                # 銘柄が存在しないなどのエラーはリトライしない
                logging.error(f"❌ {symbol} {timeframe} OHLCV取得失敗 - 取引所エラー: {e}")
                return None
            except Exception as e:
                 logging.error(f"❌ {symbol} {timeframe} OHLCV取得失敗 - 予期せぬエラー: {e}")
                 return None
                 
        if not ohlcv:
            logging.warning(f"⚠️ {symbol} {timeframe} OHLCVデータが取得できませんでした (Limit: {limit})。")
            return None
        
        # DataFrameに変換
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['datetime'] = [datetime.fromtimestamp(ts / 1000, tz=JST) for ts in df['timestamp']]
        df.set_index('datetime', inplace=True)
        df.drop('timestamp', axis=1, inplace=True)
        df.sort_index(inplace=True) 

        # データの欠落チェック (最低限の行数)
        if len(df) < limit:
            logging.warning(f"⚠️ {symbol} {timeframe}: データ数が不足しています (取得数: {len(df)} / 必要数: {limit})。")
            
        return df
        
    except Exception as e:
        logging.error(f"❌ {symbol} OHLCVデータ処理中にエラーが発生しました: {e}", exc_info=True)
        return None

async def fetch_top_volume_symbols(limit: int = TOP_SYMBOL_LIMIT) -> List[str]:
    """
    取引所のティッカー情報から、USDT建ての先物/スワップ市場で
    過去24時間の出来高に基づいたTOP銘柄を取得する。
    """
    global EXCHANGE_CLIENT
    
    if SKIP_MARKET_UPDATE:
        logging.warning("⚠️ SKIP_MARKET_UPDATEがTrueのため、出来高トップ銘柄の更新をスキップします。")
        return DEFAULT_SYMBOLS
        
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 銘柄リスト取得失敗: CCXTクライアントが準備できていません。デフォルトリストを返します。")
        return DEFAULT_SYMBOLS
        
    try:
        logging.info("⏳ 出来高トップ銘柄のリストを更新中...")
        
        # 出来高 (Volume) 情報を含むティッカーをまとめて取得
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        if not tickers:
            logging.error("❌ fetch_tickersが空のデータを返しました。デフォルトリストを返します。")
            return DEFAULT_SYMBOLS
            
        # フィルタリングとランキング
        filtered_tickers: List[Dict] = []
        for symbol, ticker in tickers.items():
            
            # NoneType Error 対策
            if ticker is None or not isinstance(ticker, dict):
                 continue
                 
            # 1. USDT建ての先物/スワップ市場 (例: BTC/USDT) のみ
            if not symbol.endswith('/USDT'):
                continue
            
            # 2. CCXTのシンボル情報に市場タイプが 'swap' または 'future' であることを確認
            market = EXCHANGE_CLIENT.markets.get(symbol)
            if not market or market.get('type') not in ['swap', 'future'] or market.get('active') is False:
                continue

            # 3. 24時間出来高 (quote volume) が存在すること
            quote_volume = ticker.get('quoteVolume') # USDT建てでの出来高
            if quote_volume is not None and quote_volume > 0:
                filtered_tickers.append({
                    'symbol': symbol,
                    'quoteVolume': quote_volume
                })
            
        # 出来高で降順にソート
        sorted_tickers = sorted(filtered_tickers, key=lambda x: x['quoteVolume'], reverse=True)
        
        # TOP Nのシンボルリストを作成
        top_symbols = [t['symbol'] for t in sorted_tickers[:limit]]
        
        # 出来高上位リストに、念のためデフォルトの主要銘柄を追加 (重複は自動で除去)
        final_symbols = list(set(top_symbols + DEFAULT_SYMBOLS))
        
        logging.info(f"✅ 出来高上位 {limit} 銘柄を取得しました。合計監視銘柄数: {len(final_symbols)} (TOP {len(top_symbols)} + Default {len(DEFAULT_SYMBOLS)})")
        return final_symbols
        
    except ccxt.DDoSProtection as e:
        logging.error(f"❌ レートリミット違反: {e}。銘柄リスト更新をスキップ。")
        return DEFAULT_SYMBOLS
    except Exception as e:
        logging.error(f"❌ 出来高トップ銘柄の取得中にエラーが発生しました: {e}", exc_info=True)
        # 致命的なエラーの場合はデフォルトリストを使用
        return DEFAULT_SYMBOLS

async def fetch_fgi_data() -> Dict:
    """Fear & Greed Index (FGI) を取得し、スコアリング用のプロキシ値を計算する"""
    global FGI_API_URL, GLOBAL_MACRO_CONTEXT
    
    # 過去データから更新間隔が短い場合はスキップ (FGIは1日1回更新が基本)
    current_fgi_value = GLOBAL_MACRO_CONTEXT.get('fgi_raw_value', 'N/A')
    if current_fgi_value != 'N/A' and time.time() - LAST_SUCCESS_TIME < 60 * 60 * 4: # 4時間以内ならスキップ
        logging.info("ℹ️ FGIデータは直近で取得済みのため更新をスキップします。")
        return GLOBAL_MACRO_CONTEXT
        
    try:
        logging.info("⏳ FGI (恐怖・貪欲指数) を取得中...")
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        # データの検証
        if not data.get('data') or not data['data'][0].get('value'):
            logging.warning("⚠️ FGI APIから有効なデータが取得できませんでした。")
            return GLOBAL_MACRO_CONTEXT
            
        fgi_data = data['data'][0]
        value = int(fgi_data['value'])
        
        # FGI値 (0-100) を -0.5 から +0.5 の範囲に正規化し、さらに -0.05 から +0.05 の範囲のボーナス/ペナルティに調整
        # 50 = 0.0 (中立), 0 = -0.05 (極度の恐怖), 100 = +0.05 (極度の貪欲)
        fgi_proxy = (value - 50) / 100.0 * (FGI_PROXY_BONUS_MAX * 2) 
        
        GLOBAL_MACRO_CONTEXT['fgi_proxy'] = fgi_proxy
        GLOBAL_MACRO_CONTEXT['fgi_raw_value'] = f"{value} ({fgi_data['value_classification']})"
        logging.info(f"✅ FGIデータ取得完了: {GLOBAL_MACRO_CONTEXT['fgi_raw_value']} -> Proxy: {fgi_proxy:+.4f}")

        # 現在のところ、為替ボーナスは恒久的に 0.0 とする
        GLOBAL_MACRO_CONTEXT['forex_bonus'] = 0.0 
        
        return GLOBAL_MACRO_CONTEXT
        
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ FGIデータ取得エラー: {e}")
        return GLOBAL_MACRO_CONTEXT
    except Exception as e:
        logging.error(f"❌ FGIデータ処理エラー: {e}", exc_info=True)
        return GLOBAL_MACRO_CONTEXT


# ====================================================================================
# TECHNICAL ANALYSIS & SCORING LOGIC
# ====================================================================================

def calculate_technical_indicators(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """OHLCVデータにテクニカル指標を追加する"""
    
    # 1. ボリンジャーバンド (BBAND)
    # df.ta.bbands(length=20, append=True) # デフォルト値
    df.ta.bbands(length=20, std=2, append=True)
    
    # 2. RSI (Relative Strength Index)
    df.ta.rsi(length=14, append=True) 
    
    # 3. MACD (Moving Average Convergence Divergence)
    df.ta.macd(fast=12, slow=26, signal=9, append=True)
    
    # 4. SMA (Simple Moving Average) - 長期トレンド判断用
    df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True, alias=f'SMA_{LONG_TERM_SMA_LENGTH}')
    
    # 5. ATR (Average True Range) - ボラティリティ/リスク管理用
    df.ta.atr(length=ATR_LENGTH, append=True) 

    # 6. OBV (On-Balance Volume) - 出来高モメンタム確認用
    df.ta.obv(append=True)

    # 7. VWAP (Volume-Weighted Average Price) - 基準価格判断用 (VWAPは通常、日中取引に使用されるが、ここでは便宜的に過去N期間で使用)
    df.ta.vwap(append=True) # デフォルトで期間は全期間

    # 8. ダイバージェンス判断 (RSIの過去N期間のピーク/ボトムを簡素化して取得)
    # (ここでは、単純に過去X期間のRSIの傾きとCloseの傾きを比較する簡易版を実装)
    
    return df

def get_latest_data_and_indicators(df: pd.DataFrame) -> Optional[Dict]:
    """最新の行のデータを辞書として返す"""
    if df is None or df.empty:
        return None
    
    # 最後の行を抽出
    latest_row = df.iloc[-1]
    
    # NaNチェック
    if latest_row.isnull().any():
        # logging.warning("⚠️ 最新のテクニカルデータにNaNが含まれています。スキップします。")
        return None
        
    return latest_row.to_dict()


def calculate_signal_score(
    df_map: Dict[str, pd.DataFrame], 
    symbol: str, 
    macro_context: Dict, 
    latest_price: float
) -> List[Dict]:
    """
    複数の時間枠のテクニカル分析に基づいて、
    統合されたシグナルスコアを計算する。

    スコアの重み付け (例):
    - 基本構造 (0.40)
    - 長期トレンドとの一致/逆行 (0.20)
    - 中期モメンタム (RSI/MACD) (0.25)
    - ボラティリティ/流動性 (0.10)
    - マクロ環境 (FGI) (0.05)
    ----------------------
    合計: 1.00
    """
    
    # 各時間枠の最新データとスコアリングを格納
    frame_scores: List[Dict] = []
    
    # マクロ環境のボーナス/ペナルティ
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    
    # FGI/Forexの合計影響度
    sentiment_bonus = fgi_proxy + forex_bonus # 最大 FGI_PROXY_BONUS_MAX の範囲に収まる
    
    # 出来高ボーナス (流動性)
    # 監視銘柄数に基づき、TOP銘柄にボーナスを与える (例えば、TOP10なら最大 LIQUIDITY_BONUS_MAX)
    # シンボルが CURRENT_MONITOR_SYMBOLS の上位にいるほどボーナスを付与する
    try:
        rank = CURRENT_MONITOR_SYMBOLS.index(symbol) + 1 if symbol in CURRENT_MONITOR_SYMBOLS else TOP_SYMBOL_LIMIT + 1
        # ランキング上位ほどボーナスが高い (線形または対数)
        liquidity_bonus_value = max(0.0, LIQUIDITY_BONUS_MAX * (1 - (rank / (TOP_SYMBOL_LIMIT * 1.5))))
    except:
         liquidity_bonus_value = 0.0 # エラー時はボーナスなし

    
    for timeframe in TARGET_TIMEFRAMES:
        df = df_map.get(timeframe)
        if df is None or len(df) < REQUIRED_OHLCV_LIMITS[timeframe] // 2: # 最低限のデータチェック
            logging.warning(f"⚠️ {symbol} {timeframe}: データ不足のためスキップします。")
            continue
            
        latest = get_latest_data_and_indicators(df)
        if latest is None:
            continue
        
        # --- スコアリングの初期化 ---
        long_score = BASE_SCORE 
        short_score = BASE_SCORE
        tech_data: Dict[str, Any] = {}

        # ----------------------------------------------------
        # 1. 長期トレンドとの一致/逆行 (SMA_200)
        # ----------------------------------------------------
        sma_200 = latest.get(f'SMA_{LONG_TERM_SMA_LENGTH}')
        
        long_term_reversal_penalty_value = 0.0
        
        if sma_200 is not None:
            # 価格が200SMAより上: ロングにボーナス / ショートにペナルティ
            if latest_price > sma_200:
                long_score += LONG_TERM_REVERSAL_PENALTY
                short_score -= LONG_TERM_REVERSAL_PENALTY
                long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY
            # 価格が200SMAより下: ショートにボーナス / ロングにペナルティ
            elif latest_price < sma_200:
                long_score -= LONG_TERM_REVERSAL_PENALTY
                short_score += LONG_TERM_REVERSAL_PENALTY
                long_term_reversal_penalty_value = -LONG_TERM_REVERSAL_PENALTY
        
        tech_data['long_term_reversal_penalty_value'] = long_term_reversal_penalty_value


        # ----------------------------------------------------
        # 2. 中期モメンタム (RSI & MACD)
        # ----------------------------------------------------
        
        # MACD (シグナル線とのクロス)
        macd_val = latest.get('MACDh_12_26_9') # MACD ヒストグラム
        
        macd_penalty_value = 0.0
        if macd_val is not None:
            if macd_val > 0:
                # ロング方向への加速 (MACD線がシグナル線より上)
                long_score += MACD_CROSS_PENALTY
                macd_penalty_value = MACD_CROSS_PENALTY
            elif macd_val < 0:
                 # ショート方向への加速 (MACD線がシグナル線より下)
                short_score += MACD_CROSS_PENALTY
                macd_penalty_value = -MACD_CROSS_PENALTY
        
        tech_data['macd_penalty_value'] = macd_penalty_value
        
        # RSI (モメンタム加速ゾーン)
        rsi = latest.get('RSI_14')
        rsi_momentum_bonus_value = 0.0
        if rsi is not None:
            if rsi > 60:
                # 過熱気味だが、モメンタムが強いことを示す
                long_score += (RSI_MOMENTUM_LOW * 0.5) 
                rsi_momentum_bonus_value = (RSI_MOMENTUM_LOW * 0.5) 
            elif rsi < 40:
                # 売られすぎ気味だが、ショートのモメンタムが強いことを示す
                short_score += (RSI_MOMENTUM_LOW * 0.5)
                rsi_momentum_bonus_value = (RSI_MOMENTUM_LOW * 0.5)
            # 40-60の間は中立
        
        tech_data['rsi_momentum_bonus_value'] = rsi_momentum_bonus_value


        # ----------------------------------------------------
        # 3. ボラティリティ/リスク管理 (BBands, ATR)
        # ----------------------------------------------------
        
        # ボラティリティ過熱ペナルティ (BB幅が急拡大している場合)
        bb_upper = latest.get('BBU_20_2.0')
        bb_lower = latest.get('BBL_20_2.0')
        
        volatility_penalty_value = 0.0
        if bb_upper is not None and bb_lower is not None:
            bb_width_rate = (bb_upper - bb_lower) / latest_price
            
            # ボラティリティが閾値を超えて急激に拡大している場合、ペナルティ
            if bb_width_rate > VOLATILITY_BB_PENALTY_THRESHOLD:
                # ペナルティを両サイドに適用
                penalty = abs((bb_width_rate - VOLATILITY_BB_PENALTY_THRESHOLD) * 5.0) # 5.0は調整係数
                penalty = min(penalty, 0.10) # 最大ペナルティ
                
                long_score -= penalty
                short_score -= penalty
                volatility_penalty_value = -penalty

        tech_data['volatility_penalty_value'] = volatility_penalty_value
        tech_data['atr_value'] = latest.get(f'ATR_{ATR_LENGTH}', 0.0) # ATR値は後でSL/TP計算に使う
        
        # ----------------------------------------------------
        # 4. 出来高による確証 (OBV)
        # ----------------------------------------------------
        # OBVの過去N期間の傾きを計算
        obv_df = df['OBV'].dropna()
        obv_momentum_bonus_value = 0.0

        if len(obv_df) > 50:
             # OBVの50期間の最小二乗法による傾き
             obv_recent = obv_df.iloc[-50:]
             x = np.arange(len(obv_recent))
             slope, _, _, _, _ = np.polyfit(x, obv_recent.values, 1, full=False)
             
             if slope > 0 and latest_price > df['close'].iloc[-50:-1].mean():
                 # 上昇トレンド + 出来高上昇 = ロングの確証
                 long_score += OBV_MOMENTUM_BONUS
                 obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
             elif slope < 0 and latest_price < df['close'].iloc[-50:-1].mean():
                 # 下降トレンド + 出来高下降 = ショートの確証
                 short_score += OBV_MOMENTUM_BONUS
                 obv_momentum_bonus_value = -OBV_MOMENTUM_BONUS

        tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus_value


        # ----------------------------------------------------
        # 5. マクロ環境と構造的ボーナス (全スコアに一律適用)
        # ----------------------------------------------------
        
        # 構造的優位性 (ベーススコアに含まれるが、詳細として分離)
        long_score += STRUCTURAL_PIVOT_BONUS
        short_score += STRUCTURAL_PIVOT_BONUS
        tech_data['structural_pivot_bonus'] = STRUCTURAL_PIVOT_BONUS
        
        # 流動性ボーナス
        long_score += liquidity_bonus_value
        short_score += liquidity_bonus_value
        tech_data['liquidity_bonus_value'] = liquidity_bonus_value
        
        # マクロ環境ボーナス/ペナルティ (FGI)
        long_score += sentiment_bonus
        short_score += sentiment_bonus
        tech_data['sentiment_fgi_proxy_bonus'] = sentiment_bonus

        
        # --- 結果の格納 ---
        frame_scores.append({
            'symbol': symbol,
            'timeframe': timeframe,
            'close_price': latest_price,
            'long_score': max(0.0, min(1.0, long_score)),  # 0.0 - 1.0 にクリッピング
            'short_score': max(0.0, min(1.0, short_score)), # 0.0 - 1.0 にクリッピング
            'tech_data': tech_data, 
        })
        
    return frame_scores


def calculate_dynamic_sltp(
    signal: Dict, 
    ohlcv_data: Dict[str, pd.DataFrame]
) -> Dict:
    """
    ATRに基づき、動的なストップロス(SL)とテイクプロフィット(TP)を計算し、
    シグナル情報に追加する。
    """
    
    side = signal['side']
    entry_price = signal['close_price'] 
    
    # 💡 重要な修正: SL/TPの計算には、最も信頼性の高いとされる高次の時間枠 (1h/4h) のATRを使用
    # 複数の時間枠のATRの平均または最大値を使用することも可能
    
    # 1h の ATR を優先的に使用
    df_1h = ohlcv_data.get('1h')
    if df_1h is None or f'ATR_{ATR_LENGTH}' not in df_1h.columns:
        # 1hがなければ、4hを試す
        df_4h = ohlcv_data.get('4h')
        if df_4h is None or f'ATR_{ATR_LENGTH}' not in df_4h.columns:
             logging.error("❌ SL/TP計算に必要な1h/4hのATRデータがありません。")
             signal['entry_price'] = entry_price
             signal['stop_loss'] = 0.0
             signal['take_profit'] = 0.0
             signal['liquidation_price'] = 0.0
             signal['rr_ratio'] = 0.0
             return signal
        atr_df = df_4h
    else:
        atr_df = df_1h
        
    # 最新のATR値を取得
    latest_atr = atr_df[f'ATR_{ATR_LENGTH}'].iloc[-1]
    
    # SL幅 (リスク幅) を計算: SL_MULTIPLIER * ATR または MIN_RISK_PERCENT (最低リスク)
    # SLの絶対値 (USDT) で計算するのではなく、価格に対する変動幅として計算
    
    # ATRに基づいたSL幅 (価格変動幅)
    atr_based_risk_price = latest_atr * ATR_MULTIPLIER_SL
    
    # 最低リスク幅 (価格のパーセンテージ)
    min_risk_price = entry_price * MIN_RISK_PERCENT 
    
    # 最終的な SL幅 (大きい方を取ることで、流動性の低い市場でも最低限のリスクを取る)
    risk_price_width = max(atr_based_risk_price, min_risk_price)
    
    # TP幅 (リワード幅) を計算: RRR * SL幅
    reward_price_width = risk_price_width * RR_RATIO_TARGET 
    
    stop_loss = 0.0
    take_profit = 0.0

    if side == 'long':
        stop_loss = entry_price - risk_price_width
        take_profit = entry_price + reward_price_width
    elif side == 'short':
        stop_loss = entry_price + risk_price_width
        take_profit = entry_price - reward_price_width
        
    # 0未満にならないように調整
    stop_loss = max(0.0, stop_loss)
    take_profit = max(0.0, take_profit)
    
    # 清算価格の計算 (通知用)
    liquidation_price = calculate_liquidation_price(
        entry_price=entry_price, 
        leverage=LEVERAGE, 
        side=side, 
        maintenance_margin_rate=MIN_MAINTENANCE_MARGIN_RATE
    )
    
    # シグナル情報に追加して返す
    signal['entry_price'] = entry_price
    signal['stop_loss'] = stop_loss
    signal['take_profit'] = take_profit
    signal['liquidation_price'] = liquidation_price
    signal['rr_ratio'] = RR_RATIO_TARGET # 固定RRRを使用

    # 実際に計算されたリスク幅 (ATR/MIN_RISKを考慮した幅) を tech_data に記録
    signal['tech_data']['risk_price_width'] = risk_price_width
    
    return signal


# ====================================================================================
# TRADING EXECUTION
# ====================================================================================

async def process_entry_signal(signal: Dict) -> Optional[Dict]:
    """
    取引シグナルに従って注文を執行する。
    """
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    if TEST_MODE:
        logging.warning(f"⚠️ TEST_MODE: {signal['symbol']} ({signal['side']}) の取引をスキップします。")
        # テストモードでは、仮想の成功結果を返す
        return {
            'status': 'ok',
            'filled_amount': FIXED_NOTIONAL_USDT / signal['entry_price'] * LEVERAGE, 
            'filled_usdt': FIXED_NOTIONAL_USDT,
            'order_id': f"TEST-{uuid.uuid4()}"
        }
        
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 取引執行失敗: CCXTクライアントが準備できていません。")
        return {'status': 'error', 'error_message': 'CCXTクライアント未準備'}
        
    side = signal['side']
    symbol = signal['symbol']
    entry_price = signal['entry_price']
    stop_loss = signal['stop_loss']
    
    # 注文サイドと数量の計算
    order_side = 'buy' if side == 'long' else 'sell'
    
    # 必要証拠金 (Initial Margin) = 固定ロット / レバレッジ
    # 名目ロット (Notional) = 固定ロット * レバレッジ
    
    # 固定ロット (FIXED_NOTIONAL_USDT) を使用し、レバレッジをかけてポジションサイズを計算
    # 注文数量 (Contracts) = (固定ロット * レバレッジ) / エントリー価格
    
    # 実際に取引所に送る名目金額 (USDT) は、FIXED_NOTIONAL_USDT
    target_notional_usdt = FIXED_NOTIONAL_USDT 
    
    # 注文数量 (Contracts/Amount) を計算
    # amount = (target_notional_usdt * LEVERAGE) / entry_price
    
    # 💡 修正: ccxtのcreate_orderのamountは、ベース通貨の数量 (契約数) であるため、
    # 数量 = 名目ロット / 現在価格 = (FIXED_NOTIONAL_USDT * LEVERAGE) / entry_price
    
    # 契約数を計算 (FIXED_NOTIONAL_USDTはUSD換算の名目価値)
    contracts_amount = (target_notional_usdt * LEVERAGE) / entry_price 
    
    if contracts_amount <= 0:
        logging.error(f"❌ 注文数量が不正です: {contracts_amount}。注文をスキップします。")
        return {'status': 'error', 'error_message': 'Amount can not be less than zero/invalid amount'}
        
    try:
        # CCXTの `create_market_order` を使用 (成行注文)
        order = await EXCHANGE_CLIENT.create_market_order(
            symbol=symbol,
            side=order_side,
            amount=contracts_amount,
            params={
                'leverage': LEVERAGE,
                'marginMode': 'cross' # Cross Margin modeを前提
            }
        )
        
        logging.info(f"✅ {symbol} {side.upper()} 注文を送信しました。注文ID: {order.get('id')}")

        # 注文が約定するのを待つ (成行注文なのでほぼ即時だが、念のため)
        await asyncio.sleep(2)
        
        # 約定情報の取得 (create_orderで直接取得できない場合)
        filled_order = order
        if order.get('status') != 'closed':
             # fetch_orderで完全な約定情報を取得する (レートリミットに注意)
             try:
                 filled_order = await EXCHANGE_CLIENT.fetch_order(order['id'], symbol)
             except Exception as e:
                 logging.warning(f"⚠️ 約定情報の取得に失敗しました (Order ID: {order.get('id')}): {e} - 原則続行")
                 pass
        
        # 約定価格、約定数量の確認
        filled_amount = filled_order.get('filled', contracts_amount)
        filled_price = filled_order.get('price', entry_price) # 約定価格
        
        if filled_amount > 0:
            filled_usdt_notional = filled_amount * filled_price # 実際の名目価値
            
            # 注文が成功した場合、OPEN_POSITIONSに新しいポジションを追加
            new_position = {
                'symbol': symbol,
                'side': side,
                'contracts': filled_amount if side == 'long' else -filled_amount, # ショートの場合はマイナスで保存
                'entry_price': filled_price,
                'filled_usdt': filled_usdt_notional,
                'liquidation_price': calculate_liquidation_price(filled_price, LEVERAGE, side, MIN_MAINTENANCE_MARGIN_RATE),
                'timestamp': int(time.time() * 1000),
                'stop_loss': stop_loss, # シグナルで計算されたSLを保持
                'take_profit': signal['take_profit'], # シグナルで計算されたTPを保持
                'leverage': LEVERAGE,
                'order_id': order.get('id'),
                # その他のシグナル情報も保存
                'score': signal['score'],
                'timeframe': signal['timeframe'],
                'rr_ratio': signal['rr_ratio'],
            }
            OPEN_POSITIONS.append(new_position)
            
            logging.info(f"✅ {symbol} ポジションを管理リストに追加しました。")
            
            # 成功した取引結果
            return {
                'status': 'ok',
                'filled_amount': filled_amount,
                'filled_price': filled_price,
                'filled_usdt': filled_usdt_notional,
                'order_id': order.get('id'),
                'new_position': new_position # 追加されたポジション情報
            }
        else:
            logging.error(f"❌ 注文は成功したが、約定数量が0でした。注文をキャンセルします。")
            # 注文が完全に約定していない場合はキャンセル
            try:
                await EXCHANGE_CLIENT.cancel_order(order['id'], symbol)
            except:
                pass
            return {'status': 'error', 'error_message': '約定数量が0'}
            
    except ccxt.InsufficientFunds as e:
        logging.error(f"❌ 取引失敗 - 資金不足: {e}")
        return {'status': 'error', 'error_message': '資金不足 (Insufficient Funds)'}
    except ccxt.ExchangeError as e:
        logging.error(f"❌ 取引失敗 - 取引所エラー: {e}")
        return {'status': 'error', 'error_message': f'取引所エラー: {e}'}
    except Exception as e:
        logging.critical(f"❌ 取引執行中の致命的エラー: {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'予期せぬエラー: {e}'}

async def close_position(position: Dict, exit_price: float, exit_type: str) -> Optional[Dict]:
    """
    ポジションを成行注文で決済する。
    """
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    if TEST_MODE:
        logging.warning(f"⚠️ TEST_MODE: {position['symbol']} ({position['side']}) の決済をスキップします。")
        # テストモードでは、仮想の成功結果を返す
        # PnLは証拠金ベースではなく、名目価値ベースで計算
        if position['side'] == 'long':
            pnl_rate = (exit_price - position['entry_price']) / position['entry_price']
        else:
             pnl_rate = (position['entry_price'] - exit_price) / position['entry_price']
             
        pnl_usdt = position['filled_usdt'] * pnl_rate 
        
        # ポジションをリストから削除 (テストモードでも削除)
        global OPEN_POSITIONS
        OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p != position]
        
        return {
            'status': 'ok',
            'exit_price': exit_price,
            'pnl_usdt': pnl_usdt,
            'pnl_rate': pnl_rate, # 証拠金に対するP&L率
            'filled_amount': abs(position['contracts']),
            'entry_price': position['entry_price'],
            'exit_type': exit_type
        }
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 決済執行失敗: CCXTクライアントが準備できていません。")
        return {'status': 'error', 'error_message': 'CCXTクライアント未準備'}
        
    symbol = position['symbol']
    side = position['side']
    contracts_amount = abs(position['contracts']) # 決済する契約数
    
    # ポジションをクローズするための注文サイド (ロングなら 'sell', ショートなら 'buy')
    order_side = 'sell' if side == 'long' else 'buy'
    
    if contracts_amount <= 0:
        logging.error(f"❌ 決済数量が不正です: {contracts_amount}。決済をスキップします。")
        # ポジション管理リストから削除のみ行う
        OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p != position]
        return {'status': 'error', 'error_message': '契約数が0または負'}
        
    try:
        # CCXTの `create_market_order` を使用 (成行注文でポジションをクローズ)
        order = await EXCHANGE_CLIENT.create_market_order(
            symbol=symbol,
            side=order_side,
            amount=contracts_amount,
            params={
                # MEXCは、ポジションをクローズするためのパラメータが必要な場合があるが、
                # create_market_orderで契約数と逆サイドを指定すれば通常はクローズできる。
            }
        )
        
        logging.info(f"✅ {symbol} {side.upper()} ポジションを {exit_type} で決済注文送信。注文ID: {order.get('id')}")
        
        # 注文が約定するのを待つ (成行注文なのでほぼ即時だが、念のため)
        await asyncio.sleep(2)
        
        # 約定情報の取得
        filled_order = order
        if order.get('status') != 'closed':
             try:
                 filled_order = await EXCHANGE_CLIENT.fetch_order(order['id'], symbol)
             except Exception as e:
                 logging.warning(f"⚠️ 決済約定情報の取得に失敗しました (Order ID: {order.get('id')}): {e} - 原則続行")
                 pass
        
        filled_price = filled_order.get('price', exit_price) # 実際はfilled_orderのpriceを使うべき
        
        # 損益の計算
        entry_price = position['entry_price']
        
        # PnLレート (名目価値に対する変動率)
        if side == 'long':
            pnl_rate = (filled_price - entry_price) / entry_price
        else: # short
            pnl_rate = (entry_price - filled_price) / entry_price
            
        # PnL USDT (名目価値に対する損益)
        pnl_usdt = position['filled_usdt'] * pnl_rate 
        
        # 決済が成功したら、OPEN_POSITIONSから削除
        OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p != position]
        
        return {
            'status': 'ok',
            'exit_price': filled_price,
            'pnl_usdt': pnl_usdt,
            'pnl_rate': pnl_rate, 
            'filled_amount': contracts_amount,
            'entry_price': entry_price, # 通知用に元の情報を追加
            'exit_type': exit_type
        }
        
    except ccxt.ExchangeError as e:
        logging.error(f"❌ 決済失敗 - 取引所エラー: {e}")
        return {'status': 'error', 'error_message': f'取引所エラー: {e}'}
    except Exception as e:
        logging.critical(f"❌ 決済執行中の致命的エラー: {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'予期せぬエラー: {e}'}

def check_position_exit_trigger(position: Dict, current_price: float) -> Optional[str]:
    """
    現在の価格に基づき、ポジションのSL/TPトリガーをチェックする。
    """
    
    symbol = position['symbol']
    side = position['side']
    stop_loss = position['stop_loss']
    take_profit = position['take_profit']
    liquidation_price = position['liquidation_price'] # 清算価格
    
    if stop_loss <= 0.0 or take_profit <= 0.0:
        logging.warning(f"⚠️ {symbol} のSL/TPが無効な値です (SL: {stop_loss}, TP: {take_profit})。監視をスキップします。")
        return None
        
    # --- 1. ストップロス (SL) トリガー ---
    if side == 'long':
        # ロング: 価格がSLを下回る
        if current_price <= stop_loss:
            logging.warning(f"🚨 SLトリガー (Long): {symbol} Price: {format_price(current_price)} <= SL: {format_price(stop_loss)}")
            return "SL_TRIGGER"
            
    elif side == 'short':
        # ショート: 価格がSLを上回る
        if current_price >= stop_loss:
            logging.warning(f"🚨 SLトリガー (Short): {symbol} Price: {format_price(current_price)} >= SL: {format_price(stop_loss)}")
            return "SL_TRIGGER"
            
    # --- 2. テイクプロフィット (TP) トリガー ---
    if side == 'long':
        # ロング: 価格がTPを上回る
        if current_price >= take_profit:
            logging.info(f"💰 TPトリガー (Long): {symbol} Price: {format_price(current_price)} >= TP: {format_price(take_profit)}")
            return "TP_TRIGGER"
            
    elif side == 'short':
        # ショート: 価格がTPを下回る
        if current_price <= take_profit:
            logging.info(f"💰 TPトリガー (Short): {symbol} Price: {format_price(current_price)} <= TP: {format_price(take_profit)}")
            return "TP_TRIGGER"

    # --- 3. 清算価格 (Liq. Price) トリガー ---
    # 清算価格は取引所側のロジックで実行されるため、BOT側では清算寸前で警告を出す程度に留める。
    # ここでは、清算価格に到達した場合は強制クローズと見なす。
    if liquidation_price > 0:
        # ロング: 現在価格が清算価格を下回る (例: 1%の余裕を持たせる)
        if side == 'long' and current_price <= liquidation_price * 1.005: 
            logging.critical(f"🚨 清算価格間近/到達 (Long): {symbol} Price: {format_price(current_price)} <= Liq: {format_price(liquidation_price)}")
            return "LIQUIDATION_RISK"
        # ショート: 現在価格が清算価格を上回る
        elif side == 'short' and current_price >= liquidation_price * 0.995: 
            logging.critical(f"🚨 清算価格間近/到達 (Short): {symbol} Price: {format_price(current_price)} >= Liq: {format_price(liquidation_price)}")
            return "LIQUIDATION_RISK"
            
    return None # トリガーなし

async def position_monitor_and_update_sltp():
    """
    オープンポジションを監視し、TP/SLトリガーをチェックして、決済注文を執行する。
    """
    global OPEN_POSITIONS
    
    if not OPEN_POSITIONS:
        return
        
    logging.info(f"⏳ ポジション監視を開始します。管理中のポジション数: {len(OPEN_POSITIONS)}")
    
    # 現在の価格を一括で取得する (レートリミット対策)
    symbols_to_fetch = list(set([p['symbol'] for p in OPEN_POSITIONS]))
    current_tickers: Dict[str, Any] = {}
    
    try:
        if EXCHANGE_CLIENT and EXCHANGE_CLIENT.has['fetchTickers']:
            tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols_to_fetch)
            for symbol, ticker in tickers.items():
                if ticker and ticker.get('last') is not None:
                     current_tickers[symbol] = ticker['last']
        else:
             # fetch_tickersがない場合は、個別にfetch_tickerで対応 (レートリミットに注意)
             for symbol in symbols_to_fetch:
                 try:
                     ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
                     if ticker and ticker.get('last') is not None:
                         current_tickers[symbol] = ticker['last']
                     await asyncio.sleep(0.5) # レートリミット対策
                 except Exception as e:
                     logging.error(f"❌ {symbol} の現在価格取得に失敗: {e}")
                     
    except Exception as e:
        logging.error(f"❌ 複数銘柄の現在価格一括取得中にエラーが発生しました: {e}")
        # エラー発生時は、処理を中断せず、次のループでリトライ
        return
        
    # 決済処理が必要なポジションを一時的に保持
    positions_to_close: List[Tuple[Dict, float, str]] = []
    
    for position in OPEN_POSITIONS:
        symbol = position['symbol']
        current_price = current_tickers.get(symbol)
        
        if current_price is None:
            logging.warning(f"⚠️ {symbol} の現在価格が取得できませんでした。スキップします。")
            continue
            
        exit_type = check_position_exit_trigger(position, current_price)
        
        if exit_type:
            # 決済が必要なポジションをリストに追加
            positions_to_close.append((position, current_price, exit_type))
            
        # 💡 [将来的な拡張] トレイリングストップロスの更新ロジックはここに追加

    # 決済が必要なポジションを処理
    for position, current_price, exit_type in positions_to_close:
        
        # 決済処理を実行
        trade_result = await close_position(position, current_price, exit_type)
        
        # 決済結果を通知
        if trade_result and trade_result.get('status') == 'ok':
            
            # 決済結果に決済価格などの情報を追加
            position['exit_price'] = trade_result['exit_price']
            position['pnl_usdt'] = trade_result['pnl_usdt']
            position['pnl_rate'] = trade_result['pnl_rate']
            position['exit_type'] = exit_type
            
            # 通知ロジック
            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT) # 通知整形のために取得
            message = format_telegram_message(position, "ポジション決済", current_threshold, trade_result)
            await send_telegram_notification(message)
            log_signal(position, "Position Exit", trade_result)
            
        elif trade_result and trade_result.get('status') == 'error':
             logging.error(f"❌ {position['symbol']} 決済失敗: {trade_result['error_message']}")
             await send_telegram_notification(f"🚨 <b>決済失敗通知</b>\n{position['symbol']} ({position['side'].upper()}) の決済に失敗しました: <code>{trade_result['error_message']}</code>")

    # 処理後にOPEN_POSITIONSが自動で更新されている (close_position内で実行)


# ====================================================================================
# MAIN BOT SCHEDULER & ENTRY POINT
# ====================================================================================

async def run_main_loop():
    """
    BOTのメインループ。定期的に市場分析と取引実行を行う。
    """
    global LAST_SUCCESS_TIME, LAST_SIGNAL_TIME, IS_FIRST_MAIN_LOOP_COMPLETED, CURRENT_MONITOR_SYMBOLS, LAST_ANALYSIS_SIGNALS

    logging.info("🚀 メイン分析ループを開始します...")
    
    # 1. 接続確認
    if not IS_CLIENT_READY:
        success = await initialize_exchange_client()
        if not success:
            logging.critical("❌ クライアント初期化失敗。メインループをスキップします。")
            # 重大なエラーの場合は、再試行の間隔を長めに設定
            await asyncio.sleep(LOOP_INTERVAL * 2) 
            return
    
    # 2. 口座ステータスとポジションの取得 (毎回の分析前に最新情報を取得)
    account_status = await fetch_account_status()
    if account_status.get('error'):
        logging.critical("❌ 口座ステータス取得失敗。取引をスキップします。")
        await asyncio.sleep(LOOP_INTERVAL * 2) 
        return

    # 3. マクロ環境データの取得 (頻度は低め)
    macro_context = await fetch_fgi_data()
    
    # 4. 監視対象銘柄のリストを更新 (頻度は低め)
    CURRENT_MONITOR_SYMBOLS = await fetch_top_volume_symbols()
    
    current_threshold = get_current_threshold(macro_context)
    
    # 5. 初回ループ完了通知 (BOTの起動完了)
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        startup_message = format_startup_message(account_status, macro_context, len(CURRENT_MONITOR_SYMBOLS), current_threshold)
        await send_telegram_notification(startup_message)
        IS_FIRST_MAIN_LOOP_COMPLETED = True
        
    # --- 市場分析とシグナル生成 ---
    
    all_signals: List[Dict] = []
    
    for symbol in CURRENT_MONITOR_SYMBOLS:
        
        # 6. クールダウンチェック: 過去12時間以内に取引シグナルが出た銘柄はスキップ
        last_signal_time = LAST_SIGNAL_TIME.get(symbol, 0.0)
        if time.time() - last_signal_time < TRADE_SIGNAL_COOLDOWN:
            # logging.debug(f"ℹ️ {symbol} はクールダウン期間中です。スキップします。")
            continue 

        # 7. 必要なOHLCVデータを取得
        ohlcv_data: Dict[str, pd.DataFrame] = {}
        tasks = []
        for tf in TARGET_TIMEFRAMES:
            tasks.append(fetch_ohlcv_data(symbol, tf, REQUIRED_OHLCV_LIMITS[tf]))
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for i, tf in enumerate(TARGET_TIMEFRAMES):
            df_or_exception = results[i]
            if isinstance(df_or_exception, pd.DataFrame) and not df_or_exception.empty:
                # 8. テクニカル指標の計算
                ohlcv_data[tf] = calculate_technical_indicators(df_or_exception, tf)
            elif isinstance(df_or_exception, Exception):
                logging.error(f"❌ {symbol} {tf} データ取得エラー: {df_or_exception}")
            
        # データが不完全な場合はスキップ
        if not ohlcv_data or '1h' not in ohlcv_data: 
             continue
        
        # 最新価格の取得
        latest_price = ohlcv_data['1h']['close'].iloc[-1]
        
        # 9. シグナルスコアの計算
        signals = calculate_signal_score(ohlcv_data, symbol, macro_context, latest_price)
        all_signals.extend(signals)
        
    
    # 10. 全てのシグナルを統合し、最高スコアのものを抽出
    
    # スコアの降順でソート
    sorted_signals = sorted(all_signals, key=lambda x: max(x.get('long_score', 0.0), x.get('short_score', 0.0)), reverse=True)
    
    # 定期通知のために最新の分析結果を保存
    LAST_ANALYSIS_SIGNALS = sorted_signals 
    
    # --- 取引シグナルをチェック ---
    
    # 既にポジションを持っている銘柄のリスト
    open_symbols = [p['symbol'] for p in OPEN_POSITIONS]
    
    for signal in sorted_signals[:TOP_SIGNAL_COUNT * 5]: # スコア上位5倍までを確認
        
        # ロング/ショートで高い方のスコアを抽出
        long_score = signal['long_score']
        short_score = signal['short_score']
        
        best_score = max(long_score, short_score)
        
        # 取引閾値を超えているかチェック
        if best_score < current_threshold:
            logging.info(f"ℹ️ {signal['symbol']} ({signal['timeframe']}): スコアが閾値 ({current_threshold*100:.0f}) 未満です ({best_score*100:.2f})。")
            continue
            
        # 既にポジションを持っている銘柄はスキップ
        if signal['symbol'] in open_symbols:
            logging.info(f"ℹ️ {signal['symbol']} は既にポジションを保有しています。スキップします。")
            continue
            
        # クールダウンチェック (念のため再チェック)
        last_signal_time = LAST_SIGNAL_TIME.get(signal['symbol'], 0.0)
        if time.time() - last_signal_time < TRADE_SIGNAL_COOLDOWN:
            continue
            
        # 最高のスコアを持つサイドを選択
        signal['side'] = 'long' if long_score > short_score else 'short'
        signal['score'] = best_score
        
        # 11. SL/TPの設定 (取引前に価格とリスクを確定させる)
        signal = calculate_dynamic_sltp(signal, ohlcv_data) # ohlcv_dataは既に計算済み
        
        # SL/TPが有効かチェック
        if signal['stop_loss'] <= 0.0 or signal['take_profit'] <= 0.0:
            logging.error(f"❌ {signal['symbol']} のSL/TP計算に失敗しました。シグナルをスキップします。")
            continue
            
        # --- 取引の実行 ---
        
        logging.warning(f"🚀 **高スコアシグナル検出** - {signal['symbol']} ({signal['side'].upper()}, {signal['timeframe']}) Score: {best_score*100:.2f}")
        
        trade_result = await process_entry_signal(signal)
        
        # 12. 結果の通知とログ記録
        if trade_result and trade_result.get('status') == 'ok':
            
            # 取引成功の場合、シグナル通知を行う
            # trade_result (filled_usdt, filled_amountなど) を通知メッセージに含める
            message = format_telegram_message(signal, "取引シグナル", current_threshold, trade_result)
            await send_telegram_notification(message)
            
            # ログに記録
            log_signal(signal, "Entry Signal", trade_result)
            
            # クールダウンタイムを更新
            LAST_SIGNAL_TIME[signal['symbol']] = time.time()
            
            # TOP_SIGNAL_COUNTで設定された数だけ取引したら、このループは終了
            if len(OPEN_POSITIONS) >= TOP_SIGNAL_COUNT:
                logging.info(f"✅ 設定された最大ポジション数 ({TOP_SIGNAL_COUNT}) に達しました。メインループを終了します。")
                break
                
        elif trade_result and trade_result.get('status') == 'error':
             logging.error(f"❌ {signal['symbol']} 取引失敗: {trade_result['error_message']}")
             
             # 取引失敗の場合も、シグナル通知は行う (エラーメッセージ付き)
             message = format_telegram_message(signal, "取引シグナル", current_threshold, trade_result)
             await send_telegram_notification(message)
             
             # ログに記録
             log_signal(signal, "Entry Signal Failed", trade_result)
             
        # APIレートリミット対策
        await asyncio.sleep(1.0) 

    # 13. 定期的な最高スコア通知
    await notify_highest_analysis_score()
    
    # 14. WebShareデータの準備
    if IS_FIRST_MAIN_LOOP_COMPLETED:
        webshare_data = {
            'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            'bot_version': BOT_VERSION,
            'exchange': CCXT_CLIENT_NAME.upper(),
            'account_equity': ACCOUNT_EQUITY_USDT,
            'monitoring_symbols_count': len(CURRENT_MONITOR_SYMBOLS),
            'open_positions_count': len(OPEN_POSITIONS),
            'open_positions': OPEN_POSITIONS, # ポジション詳細
            'macro_context': GLOBAL_MACRO_CONTEXT,
            'last_signals': sorted_signals[:10], # トップ10のシグナル
        }
        
        await send_webshare_update(webshare_data)
        
    logging.info("💤 メイン分析ループが完了しました。")
    LAST_SUCCESS_TIME = time.time()


async def main_bot_scheduler():
    """メイン分析ループを定期的に実行するスケジューラ"""
    while True:
        try:
            await run_main_loop()
        except Exception as e:
            logging.critical(f"❌ 致命的なスケジューラエラーが発生しました: {e}", exc_info=True)
            # 致命的エラー発生時も、ボットが停止しないように再試行する
            await send_telegram_notification(f"🚨 <b>致命的エラー通知</b>\nメインボットループで予期せぬエラーが発生しました: <code>{e}</code>")
        
        # 1分間隔で実行
        await asyncio.sleep(LOOP_INTERVAL) 


async def position_monitor_scheduler():
    """ポジション監視と決済トリガーのチェックをより頻繁に行うスケジューラ"""
    while True:
        try:
            # オープンポジションを監視し、TP/SLトリガーをチェック (レートリミットに注意)
            await position_monitor_and_update_sltp()
        except Exception as e:
            logging.error(f"❌ ポジション監視スケジューラでエラーが発生しました: {e}", exc_info=True)

        await asyncio.sleep(MONITOR_INTERVAL) # 10秒ごとに監視


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
