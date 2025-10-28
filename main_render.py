# ====================================================================================
# Apex BOT v20.0.43 - Future Trading / 30x Leverage 
# (Feature: 実践的スコアリングロジック、ATR動的リスク管理導入)
# 
# 🚨 致命的エラー修正強化: 
# 1. 💡 修正: np.polyfitの戻り値エラー (ValueError: not enough values to unpack) を修正
# 2. 💡 修正: fetch_tickersのAttributeError ('NoneType' object has no attribute 'keys') 対策 
# 3. 注文失敗エラー (Amount can not be less than zero) 対策
# 4. 💡 修正: 通知メッセージでEntry/SL/TP/清算価格が0になる問題を解決 (v20.0.42で対応済み)
# 5. 💡 新規: ダミーロジックを実践的スコアリングロジックに置換 (v20.0.43)
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
                    'raw_info': p, # デバッグ用
                })
                
        # グローバル変数にオープンポジションを更新
        global OPEN_POSITIONS
        OPEN_POSITIONS = open_positions

        return {
            'total_usdt_balance': total_usdt_balance,
            'open_positions': OPEN_POSITIONS,
            'error': False
        }
        
    except ccxt.ExchangeError as e:
        logging.error(f"❌ 口座ステータス取得時の取引所エラー: {e}", exc_info=True)
        return {'total_usdt_balance': 0.0, 'open_positions': OPEN_POSITIONS, 'error': True}
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得時の予期せぬエラー: {e}", exc_info=True)
        return {'total_usdt_balance': 0.0, 'open_positions': OPEN_POSITIONS, 'error': True}

async def fetch_ohlcv_data(symbol: str, timeframe: str, limit: int = 500) -> Optional[pd.DataFrame]:
    """指定されたシンボルと時間枠のOHLCVデータを取得する"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error(f"❌ OHLCV取得失敗 ({symbol}): CCXTクライアントが準備できていません。")
        return None

    try:
        # ccxtのfetch_ohlcvはタイムスタンプ (ms), open, high, low, close, volume のリストを返す
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        if not ohlcv:
            logging.warning(f"⚠️ OHLCVデータが空です: {symbol}, {timeframe}")
            return None
            
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        # タイムスタンプをJSTのdatetimeオブジェクトに変換し、インデックスとして設定
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert(JST)
        df = df.set_index('datetime')
        df.drop('timestamp', axis=1, inplace=True)
        
        # 数値型に変換
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        
        # 最後の行のtimestampから現在までの経過時間を確認
        last_candle_time = df.index[-1]
        time_diff = datetime.now(JST) - last_candle_time
        
        # タイムフレームに基づいた許容遅延時間を計算
        # 例: 1mなら2分、1hなら2時間まで許容
        # ccxtの仕様上、最後の足は不完全なことが多いので、多少の遅延は許容する
        if timeframe.endswith('m'):
            interval_minutes = int(timeframe[:-1])
            max_delay = timedelta(minutes=interval_minutes * 2)
        elif timeframe.endswith('h'):
            interval_hours = int(timeframe[:-1])
            max_delay = timedelta(hours=interval_hours * 2)
        elif timeframe.endswith('d'):
            max_delay = timedelta(days=2)
        else:
             max_delay = timedelta(minutes=3)

        # データの古さをチェック (厳密には最終足が不完全でも分析には使用するため、警告に留める)
        if time_diff > max_delay:
            logging.warning(f"⚠️ {symbol} の {timeframe} データが古いです。最終足: {last_candle_time.strftime('%H:%M:%S')} ({time_diff}前)")
            
        
        return df
        
    except ccxt.DDoSProtection as e:
        logging.error(f"❌ OHLCV取得失敗 ({symbol}, {timeframe}) - DDoS保護エラー: {e}")
    except ccxt.ExchangeNotAvailable as e:
        logging.error(f"❌ OHLCV取得失敗 ({symbol}, {timeframe}) - 取引所エラー: {e}")
    except ccxt.RequestTimeout as e:
        logging.error(f"❌ OHLCV取得失敗 ({symbol}, {timeframe}) - タイムアウトエラー: {e}")
    except Exception as e:
        # fetch_ohlcvのAttributeError ('NoneType' object has no attribute 'keys') 対策もここに含まれる
        logging.error(f"❌ OHLCV取得失敗 ({symbol}, {timeframe}) - 予期せぬエラー: {e}", exc_info=True)
        
    return None

async def fetch_top_volume_symbols(limit: int = TOP_SYMBOL_LIMIT) -> List[str]:
    """
    取引所から出来高上位のシンボルを取得し、DEFAULT_SYMBOLSとマージする。
    MEXCの場合、出来高情報が取得できないため、DEFAULT_SYMBOLSをそのまま返す。
    """
    global EXCHANGE_CLIENT, SKIP_MARKET_UPDATE
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 出来高TOPシンボル取得失敗: CCXTクライアントが準備できていません。DEFAULT_SYMBOLSを使用します。")
        return DEFAULT_SYMBOLS.copy()

    if SKIP_MARKET_UPDATE:
        logging.warning("⚠️ SKIP_MARKET_UPDATE=True のため、出来高更新をスキップし、DEFAULT_SYMBOLSを使用します。")
        return DEFAULT_SYMBOLS.copy()

    # MEXCはfetch_tickersで出来高情報を取得できないため、デフォルトを使用
    if EXCHANGE_CLIENT.id == 'mexc':
        logging.warning("⚠️ MEXCは fetch_tickers からの出来高情報が不確実なため、出来高TOP更新をスキップし、DEFAULT_SYMBOLSを使用します。")
        return DEFAULT_SYMBOLS.copy()

    try:
        # fetch_tickersのAttributeError ('NoneType' object has no attribute 'keys') 対策
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        # USDT建ての先物/スワップシンボルのみをフィルタリング
        future_usdt_tickers = {
            s: t for s, t in tickers.items() 
            if s.endswith('/USDT') and 
               t.get('info', {}).get('s', '').endswith('USDT_') and # MEXC互換 (MEXCはスキップしているが念のため)
               (t.get('type') in ['future', 'swap'] or t.get('info', {}).get('s', '').endswith('USDT_'))
        }

        # 24時間出来高 (quoteVolume, USDTでの名目出来高) でソート
        # 'quoteVolume' が存在しない場合は 0.0 と見なす
        sorted_tickers = sorted(
            future_usdt_tickers.items(), 
            key=lambda item: item[1].get('quoteVolume', 0.0) or 0.0, 
            reverse=True
        )
        
        top_symbols = [symbol for symbol, _ in sorted_tickers if symbol not in DEFAULT_SYMBOLS][:limit]
        
        # DEFAULT_SYMBOLSと重複を避けながらマージ
        final_symbols = DEFAULT_SYMBOLS.copy()
        
        # 出来高TOPのシンボルを追加
        for symbol in top_symbols:
            if symbol not in final_symbols:
                final_symbols.append(symbol)
                
        # シンボル数を TOP_SYMBOL_LIMIT に制限 (DEFAULT_SYMBOLSの要素を優先)
        final_symbols = final_symbols[:limit]
        
        logging.info(f"✅ 出来高TOP {limit} 銘柄を取得・マージしました。合計監視銘柄数: {len(final_symbols)}")
        return final_symbols
        
    except Exception as e:
        logging.error(f"❌ 出来高TOPシンボル取得に失敗しました。DEFAULT_SYMBOLSを使用します。エラー: {e}", exc_info=True)
        return DEFAULT_SYMBOLS.copy()


# ====================================================================================
# MACRO DATA ACQUISITION
# ====================================================================================

async def fetch_fear_and_greed_index() -> float:
    """
    Fear & Greed Index (FGI) を取得し、-1.0から+1.0の範囲に正規化する。
    """
    global GLOBAL_MACRO_CONTEXT, FGI_PROXY_BONUS_MAX
    
    try:
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        # APIレスポンス形式を確認
        if data.get('data') and len(data['data']) > 0:
            index_value = int(data['data'][0]['value'])
            index_value_text = data['data'][0]['value_classification']
            
            # FGI (0-100) を -1.0 (Extreme Fear) から +1.0 (Extreme Greed) に正規化
            # (X - 50) / 50 の計算ロジック
            fgi_proxy = (index_value - 50) / 50.0
            
            # グローバルコンテキストを更新
            GLOBAL_MACRO_CONTEXT['fgi_proxy'] = fgi_proxy
            GLOBAL_MACRO_CONTEXT['fgi_raw_value'] = f"{index_value} ({index_value_text})"
            
            logging.info(f"🌍 FGIを取得しました: {index_value} ({index_value_text}). 正規化値: {fgi_proxy:+.2f}")
            return fgi_proxy
            
        else:
            logging.warning("⚠️ FGI APIから有効なデータが取得できませんでした。0.0を使用します。")
            return 0.0
            
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ FGI取得失敗 (ネットワークエラー): {e}")
    except Exception as e:
        logging.error(f"❌ FGI取得失敗 (パースエラーなど): {e}", exc_info=True)
        
    return 0.0

async def update_global_macro_context() -> None:
    """
    すべてのグローバルマクロ変数を更新するメイン関数。
    """
    global GLOBAL_MACRO_CONTEXT
    
    logging.info("🌍 グローバルマクロコンテキストの更新を開始します...")
    
    fgi_proxy = await fetch_fear_and_greed_index()
    # TODO: 将来的に為替データ (DXYなど) の取得ロジックを追加し、forex_bonusを更新
    forex_bonus = 0.0 
    
    # 最終的なマクロコンテキストの更新
    GLOBAL_MACRO_CONTEXT['fgi_proxy'] = fgi_proxy
    GLOBAL_MACRO_CONTEXT['forex_bonus'] = forex_bonus
    
    logging.info(f"✅ グローバルマクロコンテキストを更新しました。FGI Proxy: {fgi_proxy:+.2f}, Forex Bonus: {forex_bonus:+.2f}")


# ====================================================================================
# TECHNICAL ANALYSIS & SCORING
# ====================================================================================

def calculate_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    指定されたデータフレームにテクニカル指標を追加する。
    pandas-taを利用。
    """
    # 1. 移動平均 (SMA)
    # 💡 SMA 200を追加 (長期トレンド判定に使用)
    df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True) 

    # 2. RSI (Relative Strength Index)
    df.ta.rsi(length=14, append=True) 

    # 3. MACD (Moving Average Convergence Divergence)
    # ta.macdは3つのカラム (MACD, MACDh, MACDs) を追加
    df.ta.macd(fast=12, slow=26, signal=9, append=True) 

    # 4. ATR (Average True Range)
    # ATRは動的SL/TP計算に不可欠
    df.ta.atr(length=ATR_LENGTH, append=True)
    
    # 5. Bollinger Bands (BBANDS)
    # ta.bbandsは3つのカラム (BBL, BBM, BBU) を追加
    df.ta.bbands(length=20, std=2.0, append=True)
    
    # 6. OBV (On-Balance Volume)
    df.ta.obv(append=True)
    
    # 7. Williams %R (WPR)
    df.ta.willr(length=14, append=True)

    return df

def score_strategy_v20(df: pd.DataFrame, timeframe: str, symbol_liquidity_rank: int, macro_context: Dict) -> Tuple[float, str, Dict]:
    """
    v20.0.43: 実践的スコアリングロジック
    複数の時間枠、テクニカル指標、市場環境を統合したスコアリングシステム。
    
    スコア構成 (合計1.00):
    - ベーススコア: 0.40 (構造的優位性)
    - 長期トレンド一致/逆行ペナルティ: +/- 0.20
    - MACDモメンタム一致/逆行ペナルティ: +/- 0.15
    - RSIモメンタム加速ボーナス: + 0.10
    - RSIダイバージェンスボーナス: + 0.10 (未実装, 予約)
    - OBV出来高確証ボーナス: + 0.04
    - ボラティリティ過熱ペナルティ: - 0.05
    - 流動性ボーナス: + 0.06
    - FGIマクロ環境ボーナス/ペナルティ: +/- 0.05
    """
    
    if df.empty or len(df) < LONG_TERM_SMA_LENGTH:
        return 0.0, "None", {}

    # 必要な列の確認（最後のローソク足）
    current = df.iloc[-1]
    
    # 辞書型に変換
    if isinstance(current, pd.Series):
        current = current.to_dict()
        
    # 最新のローソク足のデータが不完全な場合、その前の足を使う（より確実なデータ）
    if current.get('close') is None or current.get(f'RSI_{14}') is None or current.get(f'SMA_{LONG_TERM_SMA_LENGTH}') is None:
         if len(df) < 2:
             return 0.0, "None", {}
         current = df.iloc[-2].to_dict()
         
    close_price = current.get('close')
    if close_price is None or close_price == 0:
        return 0.0, "None", {}

    # ----------------------------------------------------
    # 1. 初期化とベーススコア
    # ----------------------------------------------------
    score = BASE_SCORE
    signal = "None"
    side = "neutral"
    
    tech_data = {
        'base_score': BASE_SCORE,
        'structural_pivot_bonus': STRUCTURAL_PIVOT_BONUS,
        'long_term_reversal_penalty_value': 0.0,
        'macd_penalty_value': 0.0,
        'rsi_momentum_bonus_value': 0.0,
        'obv_momentum_bonus_value': 0.0,
        'liquidity_bonus_value': 0.0,
        'sentiment_fgi_proxy_bonus': 0.0,
        'forex_bonus_value': 0.0,
        'volatility_penalty_value': 0.0,
        'atr_value': current.get(f'ATR_{ATR_LENGTH}', 0.0),
        'last_close': close_price
    }
    
    # ----------------------------------------------------
    # 2. 長期トレンドと順張り/逆張り判定 (LONG_TERM_REVERSAL_PENALTY)
    # ----------------------------------------------------
    sma_200 = current.get(f'SMA_{LONG_TERM_SMA_LENGTH}')
    
    if sma_200 and close_price:
        if close_price > sma_200:
            # 価格が長期SMAの上にある -> ロングトレンド優位
            trend_factor = LONG_TERM_REVERSAL_PENALTY # 順張りボーナスとして加算 (例: +0.20)
        elif close_price < sma_200:
            # 価格が長期SMAの下にある -> ショートトレンド優位
            trend_factor = -LONG_TERM_REVERSAL_PENALTY # 逆張りペナルティとして減算 (例: -0.20)
        else:
            trend_factor = 0.0
            
        # ロング/ショートの方向性を示すための初期設定
        if trend_factor > 0:
            side = "long"
        elif trend_factor < 0:
            side = "short"
        
        score += trend_factor
        tech_data['long_term_reversal_penalty_value'] = trend_factor

    # ----------------------------------------------------
    # 3. MACDモメンタム判定 (MACD_CROSS_PENALTY)
    # ----------------------------------------------------
    macd = current.get(f'MACD_{12}_{26}_{9}')
    macdh = current.get(f'MACDh_{12}_{26}_{9}')
    
    macd_score_factor = 0.0
    if macd and macdh and side != "neutral":
        # MACDh (ヒストグラム) の方向と長期トレンドの方向が一致するか
        if side == "long":
            if macdh > 0: # MACDヒストグラムが上昇傾向 -> モメンタム一致
                macd_score_factor = MACD_CROSS_PENALTY
            elif macdh < 0: # MACDヒストグラムが下降傾向 -> モメンタム逆行/失速
                macd_score_factor = -MACD_CROSS_PENALTY
        elif side == "short":
            if macdh < 0: # MACDヒストグラムが下降傾向 -> モメンタム一致
                macd_score_factor = MACD_CROSS_PENALTY
            elif macdh > 0: # MACDヒストグラムが上昇傾向 -> モメンタム逆行/失速
                macd_score_factor = -MACD_CROSS_PENALTY
        
        score += macd_score_factor
        tech_data['macd_penalty_value'] = macd_score_factor

    # ----------------------------------------------------
    # 4. RSIモメンタム加速ボーナス (RSI_MOMENTUM_LOW)
    # ----------------------------------------------------
    rsi = current.get(f'RSI_{14}')
    rsi_bonus = 0.0
    if rsi is not None and side != "neutral":
        if side == "long" and rsi > RSI_MOMENTUM_LOW and rsi < 70: 
            # ロングトレンドでRSIが40-70の適正水準・加速水準にある場合
            rsi_bonus = 0.10
        elif side == "short" and rsi < (100 - RSI_MOMENTUM_LOW) and rsi > 30: 
            # ショートトレンドでRSIが30-60の適正水準・加速水準にある場合
            rsi_bonus = 0.10
            
        score += rsi_bonus
        tech_data['rsi_momentum_bonus_value'] = rsi_bonus
        
    # ----------------------------------------------------
    # 5. OBV出来高確証ボーナス (OBV_MOMENTUM_BONUS)
    # ----------------------------------------------------
    # OBVの最新値とそのN期間前の値の比較で方向性の一致を判定
    obv_bonus = 0.0
    obv = df[f'OBV'].iloc[-1]
    # 3期間前のOBVと比較
    if len(df) >= 3:
        obv_prev = df[f'OBV'].iloc[-3]
        if side == "long" and obv > obv_prev:
            obv_bonus = OBV_MOMENTUM_BONUS # 出来高が上昇傾向でロングを確証
        elif side == "short" and obv < obv_prev:
            obv_bonus = OBV_MOMENTUM_BONUS # 出来高が下降傾向でショートを確証
            
        score += obv_bonus
        tech_data['obv_momentum_bonus_value'] = obv_bonus

    # ----------------------------------------------------
    # 6. 流動性ボーナス (LIQUIDITY_BONUS_MAX)
    # ----------------------------------------------------
    liquidity_bonus = 0.0
    if symbol_liquidity_rank > 0 and symbol_liquidity_rank <= TOP_SYMBOL_LIMIT:
        # ランキングが高いほどボーナスを大きくする (指数関数的減衰をイメージ)
        # ランキング1位で最大、40位で最小 (ほぼ0)
        # 例: 40位 - rank / 40 * MAX_BONUS
        normalized_rank = (TOP_SYMBOL_LIMIT - symbol_liquidity_rank) / TOP_SYMBOL_LIMIT
        liquidity_bonus = normalized_rank * LIQUIDITY_BONUS_MAX
        score += liquidity_bonus
        tech_data['liquidity_bonus_value'] = liquidity_bonus

    # ----------------------------------------------------
    # 7. FGIマクロ環境ボーナス/ペナルティ (FGI_PROXY_BONUS_MAX)
    # ----------------------------------------------------
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    # FGI Proxy (-1.0 to +1.0) を元にボーナスを計算
    fgi_score_factor = 0.0
    if side == "long":
        # FGIがプラス (Greed) 側に寄っているほどボーナス
        fgi_score_factor = max(0.0, fgi_proxy) * FGI_PROXY_BONUS_MAX
    elif side == "short":
        # FGIがマイナス (Fear) 側に寄っているほどボーナス
        fgi_score_factor = abs(min(0.0, fgi_proxy)) * FGI_PROXY_BONUS_MAX
        
    # FGI Proxyがトレンドと逆の場合、ペナルティとして機能させる
    # 例: ロングトレンドだがFGIが極端なFear (-1.0) の場合
    if side == "long" and fgi_proxy < FGI_SLUMP_THRESHOLD:
        fgi_score_factor -= FGI_PROXY_BONUS_MAX 
    elif side == "short" and fgi_proxy > FGI_ACTIVE_THRESHOLD:
        fgi_score_factor -= FGI_PROXY_BONUS_MAX
        
    score += fgi_score_factor
    tech_data['sentiment_fgi_proxy_bonus'] = fgi_score_factor
    
    # 構造的優位性ボーナスを最後に加算 (初期設定値: 0.05)
    score += STRUCTURAL_PIVOT_BONUS
    
    # ----------------------------------------------------
    # 8. ボラティリティ過熱ペナルティ (VOLATILITY_BB_PENALTY_THRESHOLD)
    # ----------------------------------------------------
    bbm = current.get(f'BBM_{20}_2.0')
    bbu = current.get(f'BBU_{20}_2.0')
    bbl = current.get(f'BBL_{20}_2.0')
    volatility_penalty = 0.0
    
    if bbm and bbu and bbl and close_price:
        # BB幅 (BBU - BBL) が価格 (BBM) の N% を超える場合、過熱とみなしペナルティ
        bb_width_ratio = (bbu - bbl) / bbm
        if bb_width_ratio > VOLATILITY_BB_PENALTY_THRESHOLD: # 例: 1%を超える場合
            # 比率が高いほどペナルティを大きくする (最大-0.05)
            # ペナルティ幅を VOLATILITY_BB_PENALTY_THRESHOLD の倍率で調整
            penalty_magnitude = min(1.0, (bb_width_ratio - VOLATILITY_BB_PENALTY_THRESHOLD) / VOLATILITY_BB_PENALTY_THRESHOLD)
            volatility_penalty = -0.05 * penalty_magnitude
            
            score += volatility_penalty
            tech_data['volatility_penalty_value'] = volatility_penalty

    # ----------------------------------------------------
    # 9. 最終調整とシグナル判定
    # ----------------------------------------------------
    
    # 最終スコアがマイナスにならないようにクリップ
    final_score = max(0.0, score)
    
    # 取引閾値の決定
    current_threshold = get_current_threshold(macro_context)

    # 最終的なシグナルの決定
    if final_score >= current_threshold:
        signal = "Buy" if side == "long" else "Sell"
    else:
        signal = "Neutral"

    return final_score, signal, tech_data


def calculate_risk_reward_levels(current_price: float, atr_value: float, side: str) -> Tuple[float, float, float]:
    """
    ATRベースで動的なストップロス (SL) とテイクプロフィット (TP) の価格レベルを計算する。
    """
    if atr_value <= 0 or current_price <= 0:
        return 0.0, 0.0, 0.0

    # ATRに基づくリスク幅 (SL) を計算
    risk_distance = atr_value * ATR_MULTIPLIER_SL
    
    # 🚨 最低リスク幅の適用: 
    # SL幅が最小リスクパーセント (例: 0.8%) 未満の場合、それを適用
    min_risk_distance = current_price * MIN_RISK_PERCENT
    risk_distance = max(risk_distance, min_risk_distance)

    # SL/TPの価格レベルを計算
    if side == 'long':
        stop_loss = current_price - risk_distance
        # TP距離 = SL距離 * RR_RATIO_TARGET
        reward_distance = risk_distance * RR_RATIO_TARGET
        take_profit = current_price + reward_distance
        
    elif side == 'short':
        stop_loss = current_price + risk_distance
        # TP距離 = SL距離 * RR_RATIO_TARGET
        reward_distance = risk_distance * RR_RATIO_TARGET
        take_profit = current_price - reward_distance
        
    else:
        return 0.0, 0.0, 0.0

    # 価格が負にならないように保証
    stop_loss = max(0.0, stop_loss)
    take_profit = max(0.0, take_profit)

    return stop_loss, take_profit, RR_RATIO_TARGET


async def analyze_and_score(symbols: List[str], macro_context: Dict) -> List[Dict]:
    """
    監視対象の全シンボルに対してテクニカル分析とスコアリングを実行する。
    """
    global TARGET_TIMEFRAMES, REQUIRED_OHLCV_LIMITS
    
    logging.info(f"📊 {len(symbols)} 銘柄のテクニカル分析を開始します。")
    
    symbol_scores: List[Dict] = []
    
    # 流動性ランキングを設定 (1から始まる)
    symbol_rankings = {symbol: i + 1 for i, symbol in enumerate(symbols)}

    for symbol_index, symbol in enumerate(symbols):
        symbol_liquidity_rank = symbol_rankings.get(symbol, len(symbols))
        
        # 1. 複数の時間軸のデータを取得
        df_data: Dict[str, pd.DataFrame] = {}
        data_valid = True
        
        # データの取得とインジケータ計算を並行して実行 (非同期でボトルネックを解消)
        tasks = [
            fetch_ohlcv_data(symbol, tf, REQUIRED_OHLCV_LIMITS[tf]) 
            for tf in TARGET_TIMEFRAMES
        ]
        results = await asyncio.gather(*tasks)

        for i, tf in enumerate(TARGET_TIMEFRAMES):
            df = results[i]
            if df is None or len(df) < REQUIRED_OHLCV_LIMITS[tf]:
                logging.warning(f"⚠️ {symbol} の {tf} データが不足しています。スキップします。")
                data_valid = False
                break
            
            # インジケータの計算
            df = calculate_technical_indicators(df)
            df_data[tf] = df
        
        if not data_valid:
            continue
            
        # 2. 複数の時間軸でスコアリング
        all_timeframe_scores: List[Dict] = []
        
        for tf in TARGET_TIMEFRAMES:
            df = df_data[tf]
            
            # 💡 v20.0.43: スコアリングロジックを適用
            score, signal_raw, tech_data = score_strategy_v20(df, tf, symbol_liquidity_rank, macro_context)
            
            if signal_raw == "Buy" or signal_raw == "Sell":
                side = "long" if signal_raw == "Buy" else "short"
                
                # ATR/RRRに基づいてSL/TPレベルを計算
                current_price = tech_data['last_close']
                atr_value = tech_data['atr_value']
                
                stop_loss, take_profit, rr_ratio = calculate_risk_reward_levels(current_price, atr_value, side)
                
                # シグナルとしてリストに追加
                all_timeframe_scores.append({
                    'symbol': symbol,
                    'timeframe': tf,
                    'side': side,
                    'score': score,
                    'entry_price': current_price,
                    'stop_loss': stop_loss,
                    'take_profit': take_profit,
                    'rr_ratio': rr_ratio,
                    'tech_data': tech_data
                })
        
        # 3. 最高のスコアを持つシグナルを抽出 (複数時間軸で最も確度の高いもの)
        if all_timeframe_scores:
            # スコアが最も高いものを選択
            best_signal = max(all_timeframe_scores, key=lambda x: x['score'])
            symbol_scores.append(best_signal)
            
            logging.info(f"📈 {symbol} の最高シグナル: {best_signal['side'].capitalize()} ({best_signal['timeframe']}), Score: {best_signal['score'] * 100:.2f}")


    # 4. 全銘柄のスコアを降順にソートして返す
    final_sorted_scores = sorted(symbol_scores, key=lambda x: x['score'], reverse=True)
    
    logging.info(f"✅ 全銘柄の分析が完了しました。有効なシグナル数: {len(final_sorted_scores)}")
    
    return final_sorted_scores

# ====================================================================================
# TRADING LOGIC
# ====================================================================================

async def check_for_cooldown(symbol: str) -> bool:
    """
    指定されたシンボルがクールダウン期間中かどうかを確認する。
    """
    global LAST_SIGNAL_TIME, TRADE_SIGNAL_COOLDOWN
    
    last_trade_time = LAST_SIGNAL_TIME.get(symbol, 0.0)
    time_since_last_trade = time.time() - last_trade_time
    
    if time_since_last_trade < TRADE_SIGNAL_COOLDOWN:
        cooldown_remaining = TRADE_SIGNAL_COOLDOWN - time_since_last_trade
        logging.info(f"⏳ {symbol} はクールダウン期間中です。残り: {cooldown_remaining:.0f}秒。")
        return True
    
    return False

def calculate_position_size(entry_price: float) -> Optional[float]:
    """
    固定ノミナル値 (FIXED_NOTIONAL_USDT) に基づいて、ポジションサイズ (契約数) を計算する。
    """
    if entry_price <= 0:
        logging.error("❌ ポジションサイズ計算失敗: エントリー価格がゼロ以下です。")
        return None
        
    # 必要名目価値 (USDT) / エントリー価格 = 契約数
    # FIXED_NOTIONAL_USDT (例: 20 USDT) の名目価値になるように契約数を決定
    contracts_amount = FIXED_NOTIONAL_USDT / entry_price
    
    # 契約数を丸める必要があるが、取引所の最小数量単位 (min_amount) が不明なため、ここでは単純に返す。
    # 実際の取引所API呼び出し時にCCXTが丸め処理を行うことを期待する。
    return contracts_amount

async def place_entry_order(signal: Dict) -> Dict:
    """
    取引シグナルに基づき、成行注文を発注する。
    """
    global EXCHANGE_CLIENT, TEST_MODE
    
    if TEST_MODE:
        logging.warning(f"⚠️ TEST_MODE: 取引シグナル ({signal['symbol']} {signal['side'].upper()}) は実行されません。")
        return {
            'status': 'ok',
            'filled_amount': calculate_position_size(signal['entry_price']) or 0.0,
            'filled_usdt': FIXED_NOTIONAL_USDT,
            'entry_price': signal['entry_price'],
            'message': 'TEST_MODEにより注文スキップ'
        }

    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ エントリー注文失敗: CCXTクライアントが準備できていません。")
        return {'status': 'error', 'error_message': 'CCXTクライアント未準備'}

    symbol = signal['symbol']
    side = signal['side']
    entry_price = signal['entry_price']
    
    # 契約数を計算 (名目価値ベース)
    amount = calculate_position_size(entry_price)
    if amount is None or amount <= 0:
        return {'status': 'error', 'error_message': '計算された注文数量がゼロ以下です。'}

    # 注文サイド (ccxtの'buy'または'sell')
    ccxt_side = 'buy' if side == 'long' else 'sell'
    
    # 注文タイプ: 成行 (Market)
    order_type = 'market'
    
    # 建玉の方向: 'long' または 'short' (MEXC向け)
    position_side_param = {'positionSide': side.capitalize()} 

    try:
        logging.info(f"➡️ 注文実行: {symbol} {ccxt_side.upper()} {amount:.4f} @ {order_type} (Nominal: {format_usdt(FIXED_NOTIONAL_USDT)} USDT)")
        
        # 注文執行
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type=order_type,
            side=ccxt_side,
            amount=amount,
            price=None, # 成行注文のため
            params=position_side_param # MEXC/Binance向け
        )

        # 注文結果の確認
        if order and order.get('status') in ['closed', 'fill', 'filled']:
            
            # 約定数量と約定価格を取得
            filled_amount = order.get('filled', amount)
            filled_price = order.get('price', entry_price) 
            
            # filled_priceが0の場合、last_trade_priceなどを確認するロジックが必要になる場合があるが、
            # 成行注文であれば通常はorderオブジェクトのpriceまたはaverageプロパティに格納される。
            if filled_price == 0 or filled_price is None:
                filled_price = order.get('average', entry_price)
            
            filled_usdt_notional = filled_amount * filled_price
            
            logging.info(f"✅ エントリー約定成功: {symbol} {side.upper()} @ {format_price(filled_price)}. 数量: {filled_amount:.4f} ({format_usdt(filled_usdt_notional)} USDT)")
            
            return {
                'status': 'ok',
                'filled_amount': filled_amount,
                'filled_usdt': filled_usdt_notional,
                'entry_price': filled_price,
                'message': '注文成功'
            }
        else:
            logging.error(f"❌ エントリー注文失敗: 注文が約定されませんでした。ステータス: {order.get('status') if order else 'N/A'}")
            return {'status': 'error', 'error_message': f'注文未約定: {order.get("status") if order else "APIレスポンスなし"}'}

    except ccxt.DDoSProtection as e:
        error_msg = f"DDoS保護エラー: {e}"
        logging.error(f"❌ エントリー注文失敗 ({symbol}): {error_msg}")
        return {'status': 'error', 'error_message': error_msg}
    except ccxt.InsufficientFunds as e:
        error_msg = f"資金不足エラー: {e}"
        logging.error(f"❌ エントリー注文失敗 ({symbol}): {error_msg}")
        # Telegramで緊急通知
        await send_telegram_notification(f"🚨 <b>緊急警告: 資金不足エラー</b>\n<code>{symbol}</code> の取引で資金が不足しています。\nボットを一時停止するか、残高を確認してください。")
        return {'status': 'error', 'error_message': error_msg}
    except ccxt.InvalidOrder as e:
        error_msg = f"無効な注文エラー: {e}"
        logging.error(f"❌ エントリー注文失敗 ({symbol}): {error_msg}")
        # 注文数量が最小取引単位未満の場合 (Amount can not be less than zero) などに対応
        if "Amount can not be less than zero" in str(e) or "minimum limit" in str(e):
            error_msg += " (最小取引数量の問題の可能性があります)"
        return {'status': 'error', 'error_message': error_msg}
    except Exception as e:
        error_msg = f"予期せぬエラー: {e}"
        logging.error(f"❌ エントリー注文失敗 ({symbol}): {error_msg}", exc_info=True)
        return {'status': 'error', 'error_message': error_msg}

async def close_position_order(position: Dict, exit_type: str, exit_price: float) -> Dict:
    """
    ポジション情報に基づき、ポジションを決済する注文を発注する。
    """
    global EXCHANGE_CLIENT
    
    if TEST_MODE:
        logging.warning(f"⚠️ TEST_MODE: ポジション決済 ({position['symbol']} {position['side'].upper()}) は実行されません。")
        # 損益計算をシミュレート
        entry_price = position['entry_price']
        filled_amount = abs(position['contracts'])
        side = position['side']
        
        if side == 'long':
            pnl_usdt = (exit_price - entry_price) * filled_amount
        else: # short
            pnl_usdt = (entry_price - exit_price) * filled_amount
            
        filled_usdt = abs(filled_amount * entry_price) # 名目価値
        pnl_rate = pnl_usdt / (filled_usdt / LEVERAGE) if filled_usdt > 0 else 0.0 # 証拠金ベースの損益率
        
        return {
            'status': 'ok',
            'entry_price': entry_price,
            'exit_price': exit_price,
            'filled_amount': filled_amount,
            'pnl_usdt': pnl_usdt,
            'pnl_rate': pnl_rate,
            'exit_type': exit_type,
            'message': 'TEST_MODEにより決済スキップ'
        }
        
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ ポジション決済失敗: CCXTクライアントが準備できていません。")
        return {'status': 'error', 'error_message': 'CCXTクライアント未準備'}
        
    symbol = position['symbol']
    side = position['side']
    contracts_raw = position['contracts'] # 正または負の値
    
    # 決済のための注文サイドは、建玉の逆
    # ロング(contracts > 0)を決済 -> 'sell'
    # ショート(contracts < 0)を決済 -> 'buy'
    ccxt_side = 'sell' if contracts_raw > 0 else 'buy'
    
    # 決済数量 (絶対値)
    amount_to_close = abs(contracts_raw)
    
    # 注文タイプ: 成行 (Market) で即座に決済
    order_type = 'market'
    
    # ポジションのクローズパラメータ (MEXC向け)
    position_side_param = {'positionSide': side.capitalize(), 'closePosition': True} 
    
    try:
        logging.info(f"🔥 ポジション決済実行: {symbol} {exit_type} ({side.upper()}). 注文: {ccxt_side.upper()} {amount_to_close:.4f} @ {order_type}")
        
        # 注文執行
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type=order_type,
            side=ccxt_side,
            amount=amount_to_close,
            price=None, # 成行注文のため
            params=position_side_param # MEXC/Binance向け
        )
        
        if order and order.get('status') in ['closed', 'fill', 'filled']:
            
            # 約定数量と約定価格を取得
            filled_amount = order.get('filled', amount_to_close)
            exit_price_actual = order.get('price', exit_price)
            if exit_price_actual == 0 or exit_price_actual is None:
                exit_price_actual = order.get('average', exit_price)
                
            entry_price = position['entry_price']
            
            # 損益計算 (先物取引のP&L計算)
            # PnL = (Exit Price - Entry Price) * Amount (Long)
            # PnL = (Entry Price - Exit Price) * Amount (Short)
            if position['side'] == 'long':
                pnl_usdt = (exit_price_actual - entry_price) * filled_amount
            else: # short
                pnl_usdt = (entry_price - exit_price_actual) * filled_amount
                
            filled_usdt = abs(filled_amount * entry_price) # 名目価値
            pnl_rate = pnl_usdt / (filled_usdt / LEVERAGE) if filled_usdt > 0 else 0.0 # 証拠金ベースの損益率
                
            logging.info(f"✅ ポジション決済成功: {symbol} @ {format_price(exit_price_actual)}. PnL: {format_usdt(pnl_usdt)} USDT ({pnl_rate*100:.2f}%)")
            
            return {
                'status': 'ok',
                'entry_price': entry_price,
                'exit_price': exit_price_actual,
                'filled_amount': filled_amount,
                'pnl_usdt': pnl_usdt,
                'pnl_rate': pnl_rate,
                'exit_type': exit_type,
                'message': '決済注文成功'
            }
        else:
            logging.error(f"❌ ポジション決済失敗: 注文が約定されませんでした。ステータス: {order.get('status') if order else 'N/A'}")
            return {'status': 'error', 'error_message': f'注文未約定: {order.get("status") if order else "APIレスポンスなし"}'}
            
    except Exception as e:
        error_msg = f"予期せぬエラー: {e}"
        logging.error(f"❌ ポジション決済失敗 ({symbol}): {error_msg}", exc_info=True)
        return {'status': 'error', 'error_message': error_msg}


async def manage_entry_signals(best_signals: List[Dict]) -> None:
    """
    最高のシグナルに基づき、エントリー注文の可否を判断し、実行する。
    """
    global LAST_SIGNAL_TIME, OPEN_POSITIONS, GLOBAL_MACRO_CONTEXT, ACCOUNT_EQUITY_USDT
    
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)

    # 既存ポジションがあるかチェック
    open_symbols = [p['symbol'] for p in OPEN_POSITIONS]

    for signal in best_signals:
        symbol = signal['symbol']
        side = signal['side']
        score = signal['score']
        
        # 1. 取引閾値チェック
        if score < current_threshold:
            logging.info(f"ℹ️ {symbol} ({side}) のスコア ({score*100:.2f}) が取引閾値 ({current_threshold*100:.0f}) 未満のためスキップします。")
            continue
            
        # 2. クールダウンチェック (一度取引した銘柄は一定期間取引しない)
        if await check_for_cooldown(symbol):
            continue
            
        # 3. 既にポジションを持っている銘柄はスキップ (ポートフォリオ分散のため)
        if symbol in open_symbols:
            logging.info(f"ℹ️ {symbol} は既にポジションを保有しているため、新しいエントリーをスキップします。")
            continue
            
        # 4. ポートフォリオ内の最大ポジション数チェック (ここでは1銘柄のみ許可)
        if len(OPEN_POSITIONS) >= TOP_SIGNAL_COUNT:
             logging.info(f"ℹ️ 最大ポジション数 ({TOP_SIGNAL_COUNT}) に達しているため、{symbol} のエントリーをスキップします。")
             break # 最もスコアの高いもの1つを処理したら終了

        # 5. エントリー注文の実行
        trade_result = await place_entry_order(signal)
        
        if trade_result['status'] == 'ok':
            
            # 注文成功後、ポジション情報をグローバル変数に反映させる（次のモニターで取得されるはずだが、即座に反映させる）
            # 約定価格、数量で清算価格を再計算
            filled_price = trade_result['entry_price']
            filled_amount = trade_result['filled_amount']
            filled_usdt = trade_result['filled_usdt']
            
            liquidation_price = calculate_liquidation_price(filled_price, LEVERAGE, side)

            # 新しいポジションを OPEN_POSITIONS に追加 (次のfetch_account_statusで上書きされるまでの一時的な状態)
            new_position = {
                'symbol': symbol,
                'side': side,
                'contracts': filled_amount if side == 'long' else -filled_amount,
                'entry_price': filled_price,
                'filled_usdt': filled_usdt, 
                'liquidation_price': liquidation_price,
                'timestamp': int(time.time() * 1000),
                'stop_loss': signal['stop_loss'], # シグナルから取得したSL/TPを保存
                'take_profit': signal['take_profit'],
                'leverage': LEVERAGE,
            }
            OPEN_POSITIONS.append(new_position)
            
            # クールダウン時間を更新
            LAST_SIGNAL_TIME[symbol] = time.time()
            
            # 通知とログ
            await send_telegram_notification(
                format_telegram_message(signal, "取引シグナル", current_threshold, trade_result)
            )
            log_signal(signal, "Entry Signal Executed", trade_result)
            
            # 1つ取引したら、残りのシグナルはスキップ (TOP_SIGNAL_COUNT=1 の場合)
            break
        
        else:
            # 注文失敗の場合も通知
            await send_telegram_notification(
                format_telegram_message(signal, "取引シグナル", current_threshold, trade_result)
            )
            log_signal(signal, "Entry Signal Failed", trade_result)


async def monitor_open_positions() -> None:
    """
    オープンポジションを監視し、SL/TP/清算価格に達したかどうかを確認する。
    """
    global OPEN_POSITIONS, GLOBAL_MACRO_CONTEXT, ACCOUNT_EQUITY_USDT
    
    if not OPEN_POSITIONS:
        logging.info("ℹ️ 監視対象のオープンポジションはありません。")
        return
        
    logging.info(f"🔥 {len(OPEN_POSITIONS)} 件のオープンポジションを監視しています...")
    
    symbols_to_monitor = [p['symbol'] for p in OPEN_POSITIONS]
    current_prices = {}
    
    # 監視対象銘柄の最新価格をまとめて取得
    tasks = [
        EXCHANGE_CLIENT.fetch_ticker(symbol) 
        for symbol in symbols_to_monitor 
        if EXCHANGE_CLIENT and EXCHANGE_CLIENT.has['fetchTicker']
    ]
    
    ticker_results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # 価格情報のパース
    for result in ticker_results:
        if isinstance(result, dict) and 'symbol' in result and result.get('last') is not None:
            current_prices[result['symbol']] = result['last']
        elif isinstance(result, Exception):
            logging.error(f"❌ Ticker取得エラー: {result}")


    positions_to_close: List[Tuple[Dict, str, float]] = [] # (position, exit_type, exit_price)
    
    for position in OPEN_POSITIONS:
        symbol = position['symbol']
        side = position['side']
        stop_loss = position['stop_loss']
        take_profit = position['take_profit']
        liquidation_price = position['liquidation_price']
        
        current_price = current_prices.get(symbol)
        
        if current_price is None or current_price == 0.0:
            logging.warning(f"⚠️ {symbol} の最新価格が取得できませんでした。スキップします。")
            continue
            
        
        # 1. 清算価格チェック (最優先)
        if side == 'long' and current_price <= liquidation_price:
            logging.critical(f"🔥 {symbol} - 🚨 清算価格 ({format_price(liquidation_price)}) に到達しました！")
            positions_to_close.append((position, "清算", current_price))
            continue
        elif side == 'short' and current_price >= liquidation_price:
            logging.critical(f"🔥 {symbol} - 🚨 清算価格 ({format_price(liquidation_price)}) に到達しました！")
            positions_to_close.append((position, "清算", current_price))
            continue

        # 2. ストップロス (SL) チェック
        # ロング: 価格 <= SL
        if side == 'long' and current_price <= stop_loss:
            logging.warning(f"🔥 {symbol} - ❌ SL ({format_price(stop_loss)}) に到達しました。")
            positions_to_close.append((position, "SL損切り", current_price))
            continue
        # ショート: 価格 >= SL
        elif side == 'short' and current_price >= stop_loss:
            logging.warning(f"🔥 {symbol} - ❌ SL ({format_price(stop_loss)}) に到達しました。")
            positions_to_close.append((position, "SL損切り", current_price))
            continue
            
        # 3. テイクプロフィット (TP) チェック
        # ロング: 価格 >= TP
        elif side == 'long' and current_price >= take_profit:
            logging.info(f"🔥 {symbol} - ✅ TP ({format_price(take_profit)}) に到達しました。")
            positions_to_close.append((position, "TP利益確定", current_price))
            continue
        # ショート: 価格 <= TP
        elif side == 'short' and current_price <= take_profit:
            logging.info(f"🔥 {symbol} - ✅ TP ({format_price(take_profit)}) に到達しました。")
            positions_to_close.append((position, "TP利益確定", current_price))
            continue
            
    
    # 決済処理を実行
    closed_positions_indices = []
    
    for position, exit_type, exit_price in positions_to_close:
        
        # 決済注文の実行
        trade_result = await close_position_order(position, exit_type, exit_price)
        
        if trade_result['status'] == 'ok':
            # 決済成功: グローバル変数からポジションを削除
            closed_positions_indices.append(position)
            
            # 通知とログ
            # 決済時のシグナルデータは元のpositionデータを使用
            await send_telegram_notification(
                format_telegram_message(position, "ポジション決済", get_current_threshold(GLOBAL_MACRO_CONTEXT), trade_result, exit_type)
            )
            log_signal(position, "Position Closed", trade_result)
        else:
            # 決済失敗: エラー通知 (ここではポジションは削除しない)
            await send_telegram_notification(
                format_telegram_message(position, "ポジション決済", get_current_threshold(GLOBAL_MACRO_CONTEXT), trade_result, exit_type)
            )
            log_signal(position, "Position Close Failed", trade_result)
            
    
    # 決済に成功したポジションをリストから削除
    global OPEN_POSITIONS
    OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p not in closed_positions_indices]
    
    # 念のため、更新後の口座情報を再取得
    if closed_positions_indices:
        await fetch_account_status()
        
    logging.info(f"✅ ポジション監視を完了しました。未決済ポジション数: {len(OPEN_POSITIONS)}")


# ====================================================================================
# MAIN SCHEDULERS & ENTRY POINT
# ====================================================================================

async def main_bot_scheduler():
    """
    メインの分析・エントリー実行スケジューラ。
    """
    global LAST_SUCCESS_TIME, CURRENT_MONITOR_SYMBOLS, IS_FIRST_MAIN_LOOP_COMPLETED, LAST_ANALYSIS_SIGNALS

    # 1. CCXTクライアントの初期化を試行
    if not await initialize_exchange_client():
        logging.critical("❌ 致命的エラー: CCXTクライアントの初期化に失敗しました。BOTを停止します。")
        return # BOTを停止 (FastAPIプロセスは生きている)

    # 2. アカウントステータスとマクロコンテキストを初回取得
    account_status = await fetch_account_status()
    await update_global_macro_context()
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    # 3. 監視銘柄リストの初回更新
    CURRENT_MONITOR_SYMBOLS = await fetch_top_volume_symbols()
    
    # 4. BOT起動完了通知を送信
    await send_telegram_notification(
        format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold)
    )
    
    # 5. メインループ開始
    while True:
        try:
            start_time = time.time()
            logging.info(f"----------------------------------------")
            logging.info(f"🚀 メイン分析ループを開始します (Ver: {BOT_VERSION})")
            
            # A. 監視銘柄の更新 (定期的に行う)
            if not IS_FIRST_MAIN_LOOP_COMPLETED or (time.time() - LAST_SUCCESS_TIME > WEBSHARE_UPLOAD_INTERVAL):
                 CURRENT_MONITOR_SYMBOLS = await fetch_top_volume_symbols()
            
            # B. グローバルマクロコンテキストの更新
            await update_global_macro_context()
            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
            
            # C. 全銘柄の分析とスコアリング
            best_signals = await analyze_and_score(CURRENT_MONITOR_SYMBOLS, GLOBAL_MACRO_CONTEXT)
            LAST_ANALYSIS_SIGNALS = best_signals # 定時通知のために保存
            
            # D. エントリーシグナルの管理と実行
            if best_signals:
                await manage_entry_signals(best_signals)
            
            # E. 定期的な WebShare アップロード
            await check_and_send_webshare_update()
            
            # F. 定時分析結果通知 (閾値未満の最高スコア)
            await notify_highest_analysis_score()
            
            # ループの完了
            LAST_SUCCESS_TIME = time.time()
            IS_FIRST_MAIN_LOOP_COMPLETED = True
            
            end_time = time.time()
            elapsed_time = end_time - start_time
            sleep_duration = max(0.0, LOOP_INTERVAL - elapsed_time)
            
            logging.info(f"✅ メイン分析ループ完了。所要時間: {elapsed_time:.2f}秒。次回実行まで {sleep_duration:.0f}秒待機。")
            logging.info(f"----------------------------------------")
            await asyncio.sleep(sleep_duration)

        except Exception as e:
            logging.critical(f"❌ メイン分析ループ内で致命的エラーが発生しました: {e}", exc_info=True)
            # エラー発生時の処理 (クライアント再初期化を試みる)
            await send_telegram_notification(f"🚨 <b>メインループ致命的エラー</b>\nエラーにより分析・取引処理を中断しました。再試行します。\n<code>{e}</code>")
            
            # クライアントが切断された可能性を考慮し、再初期化を試みる
            await initialize_exchange_client()
            await asyncio.sleep(MONITOR_INTERVAL * 2) # エラー後は少し長めに待機

async def position_monitor_scheduler():
    """
    オープンポジションのSL/TP監視と更新専用のスケジューラ。
    """
    # メインスケジューラがクライアント初期化を完了するまで待機
    while not IS_CLIENT_READY:
        await asyncio.sleep(1)

    while True:
        try:
            start_time = time.time()
            
            # 1. 口座ステータスの更新 (ポジション情報も含まれる)
            await fetch_account_status()
            
            # 2. オープンポジションのSL/TP到達をチェックし、決済を実行
            await monitor_open_positions()

            end_time = time.time()
            elapsed_time = end_time - start_time
            sleep_duration = max(0.0, MONITOR_INTERVAL - elapsed_time)
            
            logging.debug(f"ℹ️ ポジション監視ループ完了。所要時間: {elapsed_time:.2f}秒。次回実行まで {sleep_duration:.0f}秒待機。")
            await asyncio.sleep(sleep_duration)

        except Exception as e:
            logging.error(f"❌ ポジション監視ループでエラーが発生しました: {e}", exc_info=True)
            # クライアントが切断された可能性を考慮し、再初期化を試みる
            await initialize_exchange_client()
            await asyncio.sleep(MONITOR_INTERVAL * 2)

async def check_and_send_webshare_update():
    """
    WebShareの更新間隔をチェックし、必要であればデータを送信する。
    """
    global LAST_WEBSHARE_UPLOAD_TIME, WEBSHARE_UPLOAD_INTERVAL, OPEN_POSITIONS, GLOBAL_MACRO_CONTEXT, ACCOUNT_EQUITY_USDT
    
    current_time = time.time()
    
    if current_time - LAST_WEBSHARE_UPLOAD_TIME < WEBSHARE_UPLOAD_INTERVAL:
        return

    logging.info("📤 WebShare アップロードを実行します...")
    
    try:
        # アップロードするデータの構築
        webshare_data = {
            'timestamp_utc': datetime.now(timezone.utc).isoformat(),
            'timestamp_jst': datetime.now(JST).isoformat(),
            'bot_version': BOT_VERSION,
            'exchange_client': CCXT_CLIENT_NAME.upper(),
            'equity_usdt': ACCOUNT_EQUITY_USDT,
            'macro_context': GLOBAL_MACRO_CONTEXT,
            'open_positions': [
                {
                    'symbol': p['symbol'],
                    'side': p['side'],
                    'entry_price': p['entry_price'],
                    'filled_usdt': p['filled_usdt'],
                    'contracts': p['contracts'],
                    'stop_loss': p['stop_loss'],
                    'take_profit': p['take_profit'],
                    'liquidation_price': p['liquidation_price'],
                    'time_since_entry_s': current_time - (p['timestamp'] / 1000 if p.get('timestamp') else current_time),
                } for p in OPEN_POSITIONS
            ],
            'last_signals': [
                 {
                    'symbol': s['symbol'],
                    'side': s['side'],
                    'timeframe': s['timeframe'],
                    'score': s['score'],
                    'rr_ratio': s['rr_ratio'],
                    # tech_dataの主要なスコア要因のみを抽出して含める
                    'score_breakdown': {
                         'trend_factor': s['tech_data'].get('long_term_reversal_penalty_value', 0.0),
                         'macd_factor': s['tech_data'].get('macd_penalty_value', 0.0),
                         'rsi_bonus': s['tech_data'].get('rsi_momentum_bonus_value', 0.0),
                         'obv_bonus': s['tech_data'].get('obv_momentum_bonus_value', 0.0),
                         'volatility_penalty': s['tech_data'].get('volatility_penalty_value', 0.0),
                    }
                } for s in LAST_ANALYSIS_SIGNALS[:5] # Top 5のシグナルのみを送信
            ]
        }
        
        await send_webshare_update(webshare_data)
        
        LAST_WEBSHARE_UPLOAD_TIME = current_time
        logging.info("✅ WebShare アップロードを完了しました。")

    except Exception as e:
        logging.error(f"❌ WebShare アップロード中にエラーが発生しました: {e}", exc_info=True)


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


# if __name__ == "__main__":
#     # uvicorn.run(app, host="0.0.0.0", port=8000)
#     # 注意: このコードは外部から実行されることを想定しているため、メインブロックはコメントアウトされています。
#     # 例えば、Google Cloud Run, Heroku, などのプラットフォームのカスタムランタイムで実行される場合
#     # uvicorn.runの呼び出しは、環境設定（Procfileなど）に依存します。
#     # 例: gunicorn -w 4 -k uvicorn.workers.UvicornWorker main:app
