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
    global EXCHANGE_CLIENT, ACCOUNT_EQUITY_USDT, OPEN_POSITIONS
    
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
        
        # グローバルなポジションリストを更新
        OPEN_POSITIONS = open_positions 
        logging.info(f"📊 オープンポジション数: {len(OPEN_POSITIONS)}")

        return {
            'total_usdt_balance': ACCOUNT_EQUITY_USDT, 
            'open_positions': OPEN_POSITIONS, 
            'error': False
        }

    except Exception as e:
        logging.error(f"❌ 口座ステータス取得中のエラー: {e}", exc_info=True)
        return {'total_usdt_balance': ACCOUNT_EQUITY_USDT, 'open_positions': OPEN_POSITIONS, 'error': True}

async def get_top_symbols(limit: int = TOP_SYMBOL_LIMIT) -> List[str]:
    """
    取引所の出来高に基づいて取引量の多いシンボルを取得し、DEFAULT_SYMBOLSと結合する。
    """
    global EXCHANGE_CLIENT, IS_CLIENT_READY, DEFAULT_SYMBOLS
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ トップシンボル取得失敗: CCXTクライアントが準備できていません。")
        return DEFAULT_SYMBOLS
    
    try:
        # 💡 fetch_tickersはエラーを起こしやすいため、慎重に処理
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        if not tickers:
            raise Exception("fetch_tickers returned no data.")

        # USDT建て先物/スワップシンボルのみをフィルタリングし、出来高順にソート
        usdt_future_tickers = {}
        for symbol, ticker in tickers.items():
            
            # NoneType Error対策としてキーの存在をチェック
            if ticker is None or not isinstance(ticker, dict):
                continue
                
            market = EXCHANGE_CLIENT.markets.get(symbol)
            
            # USDT建て、かつ先物/スワップ、かつアクティブ
            if market and market['quote'] == 'USDT' and market['type'] in ['swap', 'future'] and market['active']:
                # 'quoteVolume' (出来高 * 価格) または 'baseVolume' (基本通貨出来高) のうち大きい方を使用
                volume = ticker.get('quoteVolume') or ticker.get('baseVolume')
                if volume is not None and volume > 0:
                    usdt_future_tickers[symbol] = volume

        # 出来高降順でソート
        sorted_tickers = sorted(usdt_future_tickers.items(), key=lambda item: item[1], reverse=True)
        
        # トップNのシンボル (CCXT形式: BTC/USDT) を抽出
        top_symbols = [symbol for symbol, _ in sorted_tickers[:limit]]
        
        # 既存のDEFAULT_SYMBOLSと重複を除いてマージする
        final_symbols = list(dict.fromkeys(top_symbols + DEFAULT_SYMBOLS))
        
        logging.info(f"✅ トップシンボル更新完了。出来高TOP {len(top_symbols)} 銘柄を含め、計 {len(final_symbols)} 銘柄を監視します。")
        return final_symbols
        
    except Exception as e:
        logging.error(f"❌ トップシンボル取得中のエラー: {e}", exc_info=True)
        # エラー時はデフォルトリストを使用
        return DEFAULT_SYMBOLS

async def fetch_ohlcv_data(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """
    指定されたシンボルのOHLCVデータを取得する。
    """
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error(f"❌ OHLCVデータ取得失敗 ({symbol} {timeframe}): CCXTクライアントが準備できていません。")
        return None
        
    try:
        # CCXTのfetch_ohlcvはリストを返す
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        if not ohlcv or len(ohlcv) < limit:
            logging.warning(f"⚠️ OHLCVデータが不足しています ({symbol} {timeframe})。取得数: {len(ohlcv) if ohlcv else 0}/{limit}")
            return None
            
        # DataFrameに変換
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms').dt.tz_localize(timezone.utc).dt.tz_convert(JST)
        df.set_index('datetime', inplace=True)
        df.drop('timestamp', axis=1, inplace=True)
        
        return df
        
    except ccxt.ExchangeError as e:
        logging.warning(f"❌ OHLCVデータ取得中の取引所エラー ({symbol} {timeframe}): {e}")
    except Exception as e:
        logging.error(f"❌ OHLCVデータ取得中の予期せぬエラー ({symbol} {timeframe}): {e}", exc_info=True)
        
    return None

async def get_macro_context() -> Dict:
    """
    FGI (Fear & Greed Index) とその他のマクロデータを取得・処理する。
    """
    fgi_proxy = 0.0
    fgi_raw_value = 'N/A'
    forex_bonus = 0.0 # 為替変動ボーナス (今回はダミー)
    
    try:
        # FGIの取得
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        if data and 'data' in data and len(data['data']) > 0:
            fgi_value = int(data['data'][0]['value']) # 0-100
            fgi_raw_value = f"{fgi_value} ({data['data'][0]['value_classification']})"
            
            # FGIをプロキシ値に変換 (-1.0 to +1.0)
            fgi_proxy = (fgi_value - 50) / 50.0 
            
            logging.info(f"✅ FGI取得成功: {fgi_raw_value} (Proxy: {fgi_proxy:.2f})")
            
            # FGIプロキシに基づくボーナス/ペナルティを計算 (例: -0.05 to +0.05 の範囲でスコアに影響)
            # FGIが極端な貪欲(Greed)の場合、ショートシグナルにボーナス、ロングにペナルティ
            # FGIが極端な恐怖(Fear)の場合、ロングシグナルにボーナス、ショートにペナルティ
            # ただし、GLOBAL_MACRO_CONTEXTに保存するfgi_proxyは [-1, 1] のまま

    except requests.exceptions.RequestException as e:
        logging.error(f"❌ FGIデータ取得エラー: {e}")
    except Exception as e:
        logging.error(f"❌ FGI処理中の予期せぬエラー: {e}", exc_info=True)
        
    # Forexボーナスは今回はダミー値
    
    return {
        'fgi_proxy': fgi_proxy,
        'fgi_raw_value': fgi_raw_value,
        'forex_bonus': forex_bonus
    }

# ====================================================================================
# TECHNICAL ANALYSIS & SCORING LOGIC
# ====================================================================================

def calculate_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    指定されたDataFrameにテクニカル指標を追加する。
    """
    if df is None or len(df) < 200: # 最低限のデータ長
        return None

    # SMA (Long-term trend)
    df['SMA_LT'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH)
    
    # ATR (Volatility)
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=ATR_LENGTH)
    
    # RSI (Momentum)
    df['RSI'] = ta.rsi(df['close'], length=14)
    
    # MACD (Momentum cross)
    macd_df = ta.macd(df['close'], fast=12, slow=26, signal=9)
    df = df.join(macd_df)
    
    # Bollinger Bands (Volatility & Structural Pivot)
    bb_df = ta.bbands(df['close'], length=20, std=2)
    df = df.join(bb_df)
    df['BB_WIDTH_PERCENT'] = (df['BBU_20_2.0'] - df['BBL_20_2.0']) / df['close']
    
    # OBV (Volume confirmation)
    df['OBV'] = ta.obv(df['close'], df['volume'])
    
    # OBV SMA (トレンド判定用)
    df['OBV_SMA'] = ta.sma(df['OBV'], length=20)
    
    # np.polyfitによるトレンド傾き (最後のN本を使用し、エラー対策済み)
    try:
        # np.polyfitの戻り値が一つだけの場合の対策（degree=1なので通常は2つ返る）
        N_SLOPE = 10
        if len(df) >= N_SLOPE:
            # 💡 修正: np.polyfitの戻り値エラー対策
            # df.iloc[-N_SLOPE:] のスライスでデータが不足した場合、エラーになる可能性を考慮し、try-exceptでカバー
            x = np.arange(len(df.iloc[-N_SLOPE:]))
            y_macd = df['MACDh_12_26_9'].iloc[-N_SLOPE:].values
            y_obv = df['OBV'].iloc[-N_SLOPE:].values
            
            # y_macdとy_obvにNaNが含まれている場合はスキップ
            if not np.isnan(y_macd).all() and not np.isnan(x).all():
                macd_slope, _ = np.polyfit(x, y_macd, 1)
                df['MACD_SLOPE'] = macd_slope
            else:
                df['MACD_SLOPE'] = np.nan
                
            if not np.isnan(y_obv).all() and not np.isnan(x).all():
                obv_slope, _ = np.polyfit(x, y_obv, 1)
                df['OBV_SLOPE'] = obv_slope
            else:
                 df['OBV_SLOPE'] = np.nan
        else:
            df['MACD_SLOPE'] = np.nan
            df['OBV_SLOPE'] = np.nan

    except ValueError as e:
        logging.warning(f"⚠️ np.polyfit 実行エラー: {e}")
        df['MACD_SLOPE'] = np.nan
        df['OBV_SLOPE'] = np.nan
        
    return df

def generate_entry_signal(symbol: str, data: Dict[str, pd.DataFrame], macro_context: Dict) -> Optional[Dict]:
    """
    複数の時間枠のデータを用いて統合的な取引シグナルを生成し、スコアリングする。
    """
    
    # 現在の市場環境に基づく動的取引閾値を取得
    current_threshold = get_current_threshold(macro_context)
    
    best_score = 0.0
    best_signal = None
    
    current_price = data['1m']['close'].iloc[-1]
    
    for tf in TARGET_TIMEFRAMES:
        df = data.get(tf)
        if df is None:
            continue
            
        # 最終足のデータ
        last = df.iloc[-1]
        
        # ----------------------------------------------------
        # 1. 総合スコア計算の初期化
        # ----------------------------------------------------
        score = BASE_SCORE # ベーススコア (0.40)
        tech_data = {}
        
        # ----------------------------------------------------
        # 2. テクニカル分析に基づくスコアリング
        # ----------------------------------------------------
        
        # 2.1. 長期トレンドとの方向一致 (SMA_LT)
        # 終値がSMA_LTより上/下か
        lt_trend_long = last['close'] > last['SMA_LT']
        lt_trend_short = last['close'] < last['SMA_LT']
        
        # 2.2. MACDモメンタム (MACD_SLOPE)
        macd_slope = last.get('MACD_SLOPE', np.nan)
        
        # 2.3. RSIモメンタム加速 (40-60レンジからの加速)
        rsi = last['RSI']
        
        # 2.4. ボリンジャーバンドの収縮/拡大 (BB_WIDTH_PERCENT)
        bb_width_percent = last.get('BB_WIDTH_PERCENT', np.nan)
        
        # 2.5. OBV出来高確証 (OBV_SLOPE と OBV_SMA)
        obv_slope = last.get('OBV_SLOPE', np.nan)
        obv = last['OBV']
        obv_sma = last['OBV_SMA']
        
        # ----------------------------------------------------
        # 3. ロング/ショート シグナル判定とスコア加算/減算
        # ----------------------------------------------------

        for side in ['long', 'short']:
            current_side_score = score # BASE_SCOREから開始
            side_tech_data = {}
            
            # --- トレンド一致/逆行 (LONG_TERM_REVERSAL_PENALTY) ---
            if side == 'long':
                if lt_trend_long: # 順張り
                    current_side_score += LONG_TERM_REVERSAL_PENALTY 
                    side_tech_data['long_term_reversal_penalty_value'] = LONG_TERM_REVERSAL_PENALTY
                elif lt_trend_short: # 逆張り
                    current_side_score -= LONG_TERM_REVERSAL_PENALTY 
                    side_tech_data['long_term_reversal_penalty_value'] = -LONG_TERM_REVERSAL_PENALTY
            elif side == 'short':
                if lt_trend_short: # 順張り
                    current_side_score += LONG_TERM_REVERSAL_PENALTY 
                    side_tech_data['long_term_reversal_penalty_value'] = LONG_TERM_REVERSAL_PENALTY
                elif lt_trend_long: # 逆張り
                    current_side_score -= LONG_TERM_REVERSAL_PENALTY 
                    side_tech_data['long_term_reversal_penalty_value'] = -LONG_TERM_REVERSAL_PENALTY
                    
            # --- MACDモメンタム (MACD_CROSS_PENALTY) ---
            if not np.isnan(macd_slope):
                if (side == 'long' and macd_slope > 0) or (side == 'short' and macd_slope < 0):
                    current_side_score += MACD_CROSS_PENALTY
                    side_tech_data['macd_penalty_value'] = MACD_CROSS_PENALTY
                else:
                    current_side_score -= MACD_CROSS_PENALTY
                    side_tech_data['macd_penalty_value'] = -MACD_CROSS_PENALTY

            # --- RSIモメンタム (RSI_MOMENTUM_LOW) ---
            if side == 'long' and rsi > RSI_MOMENTUM_LOW and rsi < 70:
                # RSI 40-70レンジで上昇モメンタムを確認
                current_side_score += STRUCTURAL_PIVOT_BONUS * 2 # 2倍のボーナス
                side_tech_data['rsi_momentum_bonus_value'] = STRUCTURAL_PIVOT_BONUS * 2
            elif side == 'short' and rsi < (100 - RSI_MOMENTUM_LOW) and rsi > 30:
                # RSI 30-60レンジで下降モメンタムを確認
                current_side_score += STRUCTURAL_PIVOT_BONUS * 2
                side_tech_data['rsi_momentum_bonus_value'] = STRUCTURAL_PIVOT_BONUS * 2
                
            # --- OBV出来高確証 (OBV_MOMENTUM_BONUS) ---
            # OBVがSMAを上抜け/下抜けし、かつ傾きがポジティブ/ネガティブ
            if (side == 'long' and obv > obv_sma and obv_slope > 0) or \
               (side == 'short' and obv < obv_sma and obv_slope < 0):
                current_side_score += OBV_MOMENTUM_BONUS
                side_tech_data['obv_momentum_bonus_value'] = OBV_MOMENTUM_BONUS
            
            # --- ボラティリティペナルティ (VOLATILITY_BB_PENALTY_THRESHOLD) ---
            if not np.isnan(bb_width_percent) and bb_width_percent > VOLATILITY_BB_PENALTY_THRESHOLD:
                # ボラティリティが高すぎる場合、ペナルティを課す (収縮後のブレイクアウトを好むため)
                penalty = -0.10 # 大きめのペナルティ
                current_side_score += penalty
                side_tech_data['volatility_penalty_value'] = penalty

            # --- 構造的優位性ボーナス (STRUCTURAL_PIVOT_BONUS) ---
            # ここではシンプルにベーススコアの構造的優位性部分として加算
            current_side_score += STRUCTURAL_PIVOT_BONUS
            side_tech_data['structural_pivot_bonus'] = STRUCTURAL_PIVOT_BONUS
            
            # --- 流動性ボーナス (LIQUIDITY_BONUS_MAX) ---
            # トップシンボルリストに含まれていればボーナスを付与
            # 出来高TOP40の順位に応じて0からLIQUIDITY_BONUS_MAXを線形に加算
            liquidity_bonus = 0.0
            if symbol in CURRENT_MONITOR_SYMBOLS:
                # 簡略化のため、ここでは一律のボーナスとする
                liquidity_bonus = LIQUIDITY_BONUS_MAX / 2
                current_side_score += liquidity_bonus
                side_tech_data['liquidity_bonus_value'] = liquidity_bonus
            
            # --- マクロコンテキストの影響 (FGI_PROXY_BONUS_MAX) ---
            fgi_proxy = macro_context.get('fgi_proxy', 0.0)
            
            fgi_bonus_value = 0.0
            if side == 'long' and fgi_proxy < 0:
                # ロングでFGIが恐怖 (マイナス) の場合、ボーナス
                fgi_bonus_value = -fgi_proxy * FGI_PROXY_BONUS_MAX # 最大 0.05
            elif side == 'short' and fgi_proxy > 0:
                # ショートでFGIが貪欲 (プラス) の場合、ボーナス
                fgi_bonus_value = fgi_proxy * FGI_PROXY_BONUS_MAX # 最大 0.05
            else:
                 # トレンドに逆らうマクロの場合、ペナルティ
                 fgi_bonus_value = fgi_proxy * (-FGI_PROXY_BONUS_MAX) if side == 'long' else fgi_proxy * (-FGI_PROXY_BONUS_MAX)
            
            current_side_score += fgi_bonus_value
            side_tech_data['sentiment_fgi_proxy_bonus'] = fgi_bonus_value
            
            # スコアの最大値を1.0に制限
            current_side_score = min(1.0, current_side_score)
            
            # スコアが閾値を超えているかチェック
            if current_side_score >= current_threshold and current_side_score > best_score:
                best_score = current_side_score
                
                # --- ATRに基づくSL/TPの計算 ---
                last_atr = data['1h']['ATR'].iloc[-1] if data.get('1h') is not None and 'ATR' in data['1h'].columns else last['ATR']
                
                # ATRに基づくリスク幅 (SLまでの距離)
                risk_usdt_amount = ACCOUNT_EQUITY_USDT * MIN_RISK_PERCENT # 最低リスク%
                
                # ATR_MULTIPLIER_SL を使って価格ベースのSL幅 (リスク幅) を決定
                sl_distance_price = last_atr * ATR_MULTIPLIER_SL
                
                # SL幅を価格の一定割合 (MIN_RISK_PERCENT) に基づく幅と比較し、大きい方を使用 (安全側)
                # 注: 価格ベースのSL幅 (sl_distance_price) が、リスク額に基づき計算されるSL幅 (risk_usdt_amount / (FIXED_NOTIONAL_USDT * LEVERAGE))
                # よりも大きいことを確認すべきだが、ここではATRベースの動的SLを優先する
                
                # ポジションサイズを考慮したリスク額からSL幅を逆算
                # risk_percent = sl_distance_price / entry_price * LEVERAGE
                
                # SL/TPの絶対価格を計算
                if side == 'long':
                    stop_loss = current_price - sl_distance_price
                    take_profit = current_price + (sl_distance_price * RR_RATIO_TARGET)
                else: # short
                    stop_loss = current_price + sl_distance_price
                    take_profit = current_price - (sl_distance_price * RR_RATIO_TARGET)
                    
                stop_loss = max(0.00000001, stop_loss)
                take_profit = max(0.00000001, take_profit)
                
                # 清算価格の計算 (概算)
                liquidation_price = calculate_liquidation_price(
                    current_price, 
                    LEVERAGE, 
                    side, 
                    MIN_MAINTENANCE_MARGIN_RATE
                )
                
                # 最終シグナルを構築
                best_signal = {
                    'symbol': symbol,
                    'timeframe': tf,
                    'side': side,
                    'score': best_score,
                    'current_price': current_price,
                    'entry_price': current_price, # 成行の場合、現在の価格をエントリー価格とする
                    'stop_loss': stop_loss,
                    'take_profit': take_profit,
                    'liquidation_price': liquidation_price,
                    'rr_ratio': RR_RATIO_TARGET,
                    'tech_data': side_tech_data,
                    'macro_context': macro_context,
                    'current_threshold': current_threshold
                }
                
    return best_signal

# ====================================================================================
# TRADING EXECUTION
# ====================================================================================

async def process_entry_signal(signal: Dict) -> Dict:
    """
    取引シグナルに基づき、実際に取引を実行する。
    """
    global EXCHANGE_CLIENT, IS_CLIENT_READY, LAST_SIGNAL_TIME, OPEN_POSITIONS
    
    symbol = signal['symbol']
    side = signal['side']
    current_price = signal['current_price']
    
    if not IS_CLIENT_READY or not EXCHANGE_CLIENT:
        logging.error(f"❌ 取引失敗 ({symbol}): CCXTクライアントが準備できていません。")
        return {'status': 'error', 'error_message': 'Client not ready'}
        
    if TEST_MODE:
        logging.warning(f"⚠️ 取引実行スキップ ({symbol}): TEST_MODEが有効です。")
        # テストモードでは、仮想的にポジションを追加し、成功と見なす
        virtual_contracts = FIXED_NOTIONAL_USDT / current_price 
        if side == 'short':
             virtual_contracts *= -1
             
        # ポジション情報にSL/TPも追加
        new_position = {
            'symbol': symbol,
            'side': side,
            'contracts': virtual_contracts,
            'entry_price': current_price,
            'filled_usdt': FIXED_NOTIONAL_USDT,
            'liquidation_price': signal['liquidation_price'],
            'timestamp': int(time.time() * 1000),
            'stop_loss': signal['stop_loss'],
            'take_profit': signal['take_profit'],
            'leverage': LEVERAGE,
            'raw_info': {'test_mode': True},
        }
        OPEN_POSITIONS.append(new_position)
        
        # シグナルに約定情報を追加
        signal.update({
            'filled_usdt': FIXED_NOTIONAL_USDT,
            'filled_amount': abs(virtual_contracts)
        })
        return {'status': 'ok', 'filled_usdt': FIXED_NOTIONAL_USDT, 'filled_amount': abs(virtual_contracts)}

    try:
        # ポジション名目価値 (USDT)
        notional_value = FIXED_NOTIONAL_USDT 
        
        # 注文方向
        order_side = 'buy' if side == 'long' else 'sell'
        
        # 注文数量 (CCXT形式の契約数) を計算
        # contracts = Notional Value / Current Price (約定価格) * Leverage -> 間違い。先物では通常 Notional Value / Mark Price で契約数を計算
        amount = notional_value / current_price # 概算の数量
        
        # CCXTの精度に合わせて数量を丸める
        market = EXCHANGE_CLIENT.market(symbol)
        amount = EXCHANGE_CLIENT.amount_to_precision(symbol, amount)
        
        # 🚨 注文実行 (成行 - Market Order)
        order = await EXCHANGE_CLIENT.create_order(
            symbol,
            'market', # 成行注文
            order_side,
            amount,
            params={'positionSide': side.capitalize()} # バイナンス等向け
        )

        logging.info(f"✅ 取引実行成功: {symbol} {side.upper()} {amount} ({format_usdt(notional_value)} USDT相当)")
        
        # 約定価格、約定数量の取得
        filled_price = order.get('price', current_price)
        filled_amount = order.get('filled', amount)
        filled_usdt_notional = filled_price * filled_amount # 実際の約定名目価値
        
        # グローバルなポジションリストに追加
        contracts_final = filled_amount * (1 if side == 'long' else -1)
        
        new_position = {
            'symbol': symbol,
            'side': side,
            'contracts': contracts_final,
            'entry_price': filled_price,
            'filled_usdt': filled_usdt_notional,
            'liquidation_price': calculate_liquidation_price(filled_price, LEVERAGE, side, MIN_MAINTENANCE_MARGIN_RATE),
            'timestamp': int(time.time() * 1000),
            'stop_loss': signal['stop_loss'],
            'take_profit': signal['take_profit'],
            'leverage': LEVERAGE,
            'raw_info': order,
        }
        
        # リアルタイムのSL/TPを設定 (取引所がサポートしている場合)
        if EXCHANGE_CLIENT.has['createStopMarketOrder'] or EXCHANGE_CLIENT.has['createStopLimitOrder']:
            try:
                # Stop Loss 注文
                sl_order = await EXCHANGE_CLIENT.create_order(
                    symbol, 
                    'STOP_MARKET', # ストップ成行
                    'sell' if side == 'long' else 'buy', 
                    filled_amount, 
                    params={'stopPrice': signal['stop_loss']} # トリガー価格
                )
                logging.info(f"✅ SL注文を自動設定しました: {format_price(signal['stop_loss'])}")
                new_position['sl_order_id'] = sl_order.get('id')
                
                # Take Profit 注文
                tp_order = await EXCHANGE_CLIENT.create_order(
                    symbol, 
                    'TAKE_PROFIT_MARKET', # TP成行
                    'sell' if side == 'long' else 'buy', 
                    filled_amount, 
                    params={'stopPrice': signal['take_profit']} # トリガー価格
                )
                logging.info(f"✅ TP注文を自動設定しました: {format_price(signal['take_profit'])}")
                new_position['tp_order_id'] = tp_order.get('id')

            except Exception as e:
                logging.warning(f"⚠️ SL/TP注文設定中にエラーが発生しました。ボット側で監視します。: {e}")
                
        # ポジションリストを更新
        OPEN_POSITIONS.append(new_position)
        
        # クールダウンタイマーをリセット
        LAST_SIGNAL_TIME[symbol] = time.time()
        
        # シグナル情報に最終的な約定情報を追加
        signal.update({
            'entry_price': filled_price,
            'liquidation_price': new_position['liquidation_price'],
            'filled_usdt': filled_usdt_notional,
            'filled_amount': filled_amount
        })

        return {'status': 'ok', 'filled_usdt': filled_usdt_notional, 'filled_amount': filled_amount}

    except ccxt.InsufficientFunds as e:
        logging.error(f"❌ 取引失敗 ({symbol}): 資金不足エラー。{e}")
        return {'status': 'error', 'error_message': f"資金不足エラー: {e}"}
    except ccxt.DDoSProtection as e:
        logging.error(f"❌ 取引失敗 ({symbol}): DDoS保護/レートリミットエラー。{e}")
        return {'status': 'error', 'error_message': f"レートリミットエラー: {e}"}
    except ccxt.ExchangeError as e:
        logging.error(f"❌ 取引失敗 ({symbol}): 取引所APIエラー。{e}")
        return {'status': 'error', 'error_message': f"取引所エラー: {e}"}
    except Exception as e:
        logging.critical(f"❌ 取引失敗 ({symbol}): 予期せぬ致命的エラー。{e}", exc_info=True)
        return {'status': 'error', 'error_message': f"致命的エラー: {e}"}


async def process_exit_signal(position: Dict, exit_type: str, current_price: float) -> Dict:
    """
    指定されたポジションを決済する。
    """
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    symbol = position['symbol']
    side = position['side']
    contracts = position['contracts']
    
    if not IS_CLIENT_READY or not EXCHANGE_CLIENT:
        logging.error(f"❌ 決済失敗 ({symbol}): CCXTクライアントが準備できていません。")
        return {'status': 'error', 'error_message': 'Client not ready'}

    if TEST_MODE:
        logging.warning(f"⚠️ 決済実行スキップ ({symbol}): TEST_MODEが有効です。")
        pnl_rate = (current_price - position['entry_price']) / position['entry_price'] * LEVERAGE * (1 if side == 'long' else -1)
        pnl_usdt = position['filled_usdt'] * pnl_rate
        
        # グローバルなポジションリストから削除
        # 💡 修正: global宣言が必要
        global OPEN_POSITIONS
        try:
            OPEN_POSITIONS.remove(position)
        except ValueError:
            logging.warning(f"⚠️ テストポジション {symbol} がリストに見つかりませんでした。")
            
        return {
            'status': 'ok', 
            'exit_type': exit_type,
            'exit_price': current_price,
            'pnl_usdt': pnl_usdt,
            'pnl_rate': pnl_rate,
            'entry_price': position['entry_price'],
            'filled_amount': abs(contracts)
        }


    try:
        # 決済数量 (絶対値)
        amount_to_close = abs(contracts)
        
        # 決済方向 (エントリーの逆)
        close_side = 'sell' if side == 'long' else 'buy'
        
        # 🚨 決済注文実行 (成行 - Market Order)
        order = await EXCHANGE_CLIENT.create_order(
            symbol,
            'market', 
            close_side,
            amount_to_close,
            params={'positionSide': side.capitalize()} # バイナンス等向け
        )
        
        logging.info(f"✅ 決済実行成功: {symbol} {side.upper()} ポジションを {exit_type} でクローズしました。")

        # 決済価格の取得
        exit_price = order.get('price', current_price)
        
        # 損益計算 (概算)
        # 実際の損益は取引所APIのポジション情報から取得すべきだが、ここでは概算で通知用に計算
        pnl_rate = (exit_price - position['entry_price']) / position['entry_price'] * LEVERAGE * (1 if side == 'long' else -1)
        pnl_usdt = position['filled_usdt'] * pnl_rate
        
        # グローバルなポジションリストから削除
        # 💡 修正: global宣言が必要
        global OPEN_POSITIONS
        try:
            OPEN_POSITIONS.remove(position)
        except ValueError:
            logging.error(f"❌ リアルポジション {symbol} がリストに見つかりませんでした。")
            
        # SL/TPの予約注文があればキャンセル
        if position.get('sl_order_id'):
            try:
                await EXCHANGE_CLIENT.cancel_order(position['sl_order_id'], symbol)
            except Exception as e:
                logging.warning(f"⚠️ SL注文キャンセル失敗: {e}")
        if position.get('tp_order_id'):
            try:
                await EXCHANGE_CLIENT.cancel_order(position['tp_order_id'], symbol)
            except Exception as e:
                logging.warning(f"⚠️ TP注文キャンセル失敗: {e}")
        
        return {
            'status': 'ok', 
            'exit_type': exit_type,
            'exit_price': exit_price,
            'pnl_usdt': pnl_usdt,
            'pnl_rate': pnl_rate,
            'entry_price': position['entry_price'],
            'filled_amount': abs(contracts)
        }

    except Exception as e:
        logging.critical(f"❌ 決済失敗 ({symbol}): 予期せぬ致命的エラー。{e}", exc_info=True)
        return {'status': 'error', 'error_message': f"決済致命的エラー: {e}"}


# ====================================================================================
# SCHEDULERS & MAIN LOOP (エラー修正箇所含む)
# ====================================================================================

async def _check_and_process_position_exits(current_prices: Dict[str, float]) -> None:
    """
    オープンポジションをチェックし、SL/TPに達していれば決済する。
    """
    # 💡 致命的エラー修正: global宣言を関数の先頭に移動
    global OPEN_POSITIONS
    
    positions_to_remove = []
    exit_results = []
    
    for pos in OPEN_POSITIONS:
        symbol = pos['symbol']
        side = pos['side']
        current_price = current_prices.get(symbol)
        
        if current_price is None:
            # 最新価格が取得できない場合はスキップ
            continue 
            
        sl_price = pos['stop_loss']
        tp_price = pos['take_profit']
        
        exit_type = None
        
        # SL/TPのチェック
        if side == 'long':
            if current_price <= sl_price:
                exit_type = "SL (損切り)"
            elif current_price >= tp_price:
                exit_type = "TP (利益確定)"
        elif side == 'short':
            if current_price >= sl_price:
                exit_type = "SL (損切り)"
            elif current_price <= tp_price:
                exit_type = "TP (利益確定)"
                
        if exit_type:
            logging.info(f"🚨 決済シグナル発生: {symbol} - {exit_type} (現在価格: {format_price(current_price)})")
            
            # ポジション決済処理を実行
            exit_result = await process_exit_signal(pos, exit_type, current_price)
            
            if exit_result['status'] == 'ok':
                # 決済成功の場合、通知リストに追加
                exit_results.append({
                    'signal': pos, # ポジション情報をシグナル情報として使用
                    'result': exit_result,
                    'exit_type': exit_type
                })
            
            # 決済成功・失敗に関わらず、OPEN_POSITIONSから削除される (process_exit_signal内で処理済み)

    # 決済成功したポジションの通知
    for item in exit_results:
        pos_signal = item['signal']
        pos_result = item['result']
        
        # 通知メッセージを送信
        notification_message = format_telegram_message(
            signal=pos_signal, 
            context="ポジション決済", 
            current_threshold=get_current_threshold(GLOBAL_MACRO_CONTEXT),
            trade_result=pos_result,
            exit_type=item['exit_type']
        )
        await send_telegram_notification(notification_message)
        log_signal(pos_signal, "POSITION_EXIT", pos_result)

async def position_monitor_scheduler():
    """
    オープンポジションを監視し、SL/TPトリガーをチェックする無限ループ。
    """
    global MONITOR_INTERVAL, OPEN_POSITIONS, IS_CLIENT_READY

    while True:
        try:
            if not IS_CLIENT_READY:
                logging.warning("⚠️ モニタースケジューラ待機中: クライアントが初期化されていません。")
                await asyncio.sleep(MONITOR_INTERVAL * 2)
                continue

            if not OPEN_POSITIONS:
                logging.info("ℹ️ ポジションなし: スキップします。")
                await asyncio.sleep(MONITOR_INTERVAL)
                continue

            symbols_to_monitor = [p['symbol'] for p in OPEN_POSITIONS]
            
            # 監視対象銘柄の最新価格を一括取得
            tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols_to_monitor)
            
            current_prices = {
                symbol: ticker['last'] 
                for symbol, ticker in tickers.items() 
                if ticker and ticker.get('last') is not None
            }
            
            # SL/TPチェックと決済処理を実行
            await _check_and_process_position_exits(current_prices)
            
        except ccxt.DDoSProtection as e:
            logging.error(f"❌ モニターDDoSエラー: レートリミットに達しました。一時停止します: {e}")
            await asyncio.sleep(MONITOR_INTERVAL * 5)
        except Exception as e:
            logging.error(f"❌ ポジション監視スケジューラで予期せぬエラーが発生しました: {e}", exc_info=True)
            # エラー発生時は少し長く待つ
            await asyncio.sleep(MONITOR_INTERVAL * 3)

        await asyncio.sleep(MONITOR_INTERVAL)


async def main_bot_scheduler():
    """
    BOTのメインロジック (データ取得、分析、取引実行) を定期的に実行する無限ループ。
    """
    global LOOP_INTERVAL, LAST_SUCCESS_TIME, GLOBAL_MACRO_CONTEXT, LAST_WEBSHARE_UPLOAD_TIME, IS_FIRST_MAIN_LOOP_COMPLETED, LAST_ANALYSIS_SIGNALS
    
    # 起動時の初期化
    if not await initialize_exchange_client():
        await send_telegram_notification("❌ <b>Apex BOT起動失敗</b>: CCXTクライアントの初期化に失敗しました。APIキー/シークレットを確認してください。")
        sys.exit(1) # 致命的エラーとして終了
    
    # 初回にマクロコンテキストを取得
    GLOBAL_MACRO_CONTEXT = await get_macro_context()
    
    # 初回に口座ステータスを取得し、起動通知を送信
    initial_account_status = await fetch_account_status()
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    startup_message = format_startup_message(initial_account_status, GLOBAL_MACRO_CONTEXT, len(DEFAULT_SYMBOLS), current_threshold)
    await send_telegram_notification(startup_message)

    while True:
        start_time = time.time()
        
        try:
            # 1. CCXTクライアントが準備できていなければ、再初期化を試みる
            if not IS_CLIENT_READY:
                logging.error("❌ クライアントが準備できていません。再初期化を試みます。")
                await initialize_exchange_client()
                if not IS_CLIENT_READY:
                    await asyncio.sleep(LOOP_INTERVAL * 2)
                    continue

            # 2. 監視対象銘柄の更新 (1時間ごと)
            if not IS_FIRST_MAIN_LOOP_COMPLETED or (time.time() - LAST_SUCCESS_TIME) > 60 * 60:
                global CURRENT_MONITOR_SYMBOLS
                CURRENT_MONITOR_SYMBOLS = await get_top_symbols()
                
            # 3. 口座ステータス、マクロコンテキストの更新
            await fetch_account_status()
            GLOBAL_MACRO_CONTEXT = await get_macro_context()
            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)

            # 4. 全銘柄の分析とシグナル生成
            
            all_signals: List[Dict] = []
            
            for symbol in CURRENT_MONITOR_SYMBOLS:
                
                # ポジション保有中はエントリーシグナル生成をスキップ (ここでは単一ポジション戦略を想定)
                if any(p['symbol'] == symbol for p in OPEN_POSITIONS):
                    continue 

                # クールダウンチェック
                if time.time() - LAST_SIGNAL_TIME.get(symbol, 0.0) < TRADE_SIGNAL_COOLDOWN:
                    logging.info(f"ℹ️ {symbol}: クールダウン中のためスキップします。")
                    continue
                    
                data = {}
                is_data_ok = True
                
                # 全ての時間枠のOHLCVデータを取得
                for tf, limit in REQUIRED_OHLCV_LIMITS.items():
                    df = await fetch_ohlcv_data(symbol, tf, limit)
                    if df is None:
                        is_data_ok = False
                        break
                        
                    # テクニカル指標の計算
                    df_tech = calculate_technical_indicators(df)
                    if df_tech is None:
                        is_data_ok = False
                        break
                        
                    data[tf] = df_tech
                    
                if not is_data_ok:
                    continue
                    
                # シグナル生成
                signal = generate_entry_signal(symbol, data, GLOBAL_MACRO_CONTEXT)
                
                if signal:
                    all_signals.append(signal)
            
            # 5. シグナル評価と取引実行
            
            # スコア降順にソートし、最も高いシグナルを取得
            all_signals = sorted(all_signals, key=lambda x: x['score'], reverse=True)
            
            # 💡 分析結果をグローバル変数に保存 (定期通知用)
            LAST_ANALYSIS_SIGNALS = all_signals 
            
            # 取引閾値を超えたシグナルのみをフィルタリング
            tradable_signals = [s for s in all_signals if s['score'] >= current_threshold]
            
            if tradable_signals:
                best_signal = tradable_signals[0] # 最高スコアのシグナル
                
                # ポジション数制限をチェック
                if len(OPEN_POSITIONS) < TOP_SIGNAL_COUNT:
                    logging.info(f"✅ 取引シグナル発見: {best_signal['symbol']} ({best_signal['side']}) Score: {best_signal['score']:.2f}")
                    
                    # 取引を実行
                    trade_result = await process_entry_signal(best_signal)
                    
                    # Telegramに通知
                    notification_message = format_telegram_message(
                        signal=best_signal, 
                        context="取引シグナル", 
                        current_threshold=current_threshold,
                        trade_result=trade_result
                    )
                    await send_telegram_notification(notification_message)
                    log_signal(best_signal, "TRADE_SIGNAL", trade_result)
                else:
                    logging.warning(f"⚠️ {best_signal['symbol']}: ポジション数 ({len(OPEN_POSITIONS)}) が上限 ({TOP_SIGNAL_COUNT}) に達しているため、スキップします。")
                    
            else:
                logging.info("ℹ️ 今回の分析で取引閾値を超えるシグナルはありませんでした。")

            # 6. その他の定期処理
            await notify_highest_analysis_score()
            
            # WebShareデータのアップロード (1時間ごと)
            if time.time() - LAST_WEBSHARE_UPLOAD_TIME > WEBSHARE_UPLOAD_INTERVAL:
                webshare_data = {
                    'timestamp': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
                    'equity': ACCOUNT_EQUITY_USDT,
                    'open_positions_count': len(OPEN_POSITIONS),
                    'open_positions': OPEN_POSITIONS,
                    'last_signals': all_signals[:5],
                    'macro_context': GLOBAL_MACRO_CONTEXT,
                    'bot_version': BOT_VERSION
                }
                await send_webshare_update(webshare_data)
                LAST_WEBSHARE_UPLOAD_TIME = time.time()
                
            LAST_SUCCESS_TIME = time.time()
            IS_FIRST_MAIN_LOOP_COMPLETED = True
            
        except ccxt.RateLimitExceeded as e:
            logging.error(f"❌ メインDDoSエラー: レートリミットに達しました。一時停止します: {e}")
            await asyncio.sleep(LOOP_INTERVAL * 5)
        except Exception as e:
            logging.critical(f"❌ メインBOTスケジューラで予期せぬ致命的エラーが発生しました: {e}", exc_info=True)
            await send_telegram_notification(f"🚨 <b>Apex BOT致命的エラー</b>: メインループが停止する可能性があります。詳細: <code>{e}</code>")
            # 致命的エラーの場合は待機時間を長くする
            await asyncio.sleep(LOOP_INTERVAL * 10)

        # 次のループまでの待ち時間調整
        elapsed_time = time.time() - start_time
        sleep_time = max(0, LOOP_INTERVAL - elapsed_time)
        logging.info(f"🔄 メインループ完了。次の実行まで {sleep_time:.2f} 秒待機します。")
        await asyncio.sleep(sleep_time)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI()

@app.get("/")
async def root():
    # 応答に現在のポジション数を含める
    return {
        "message": f"Apex BOT {BOT_VERSION} is running.",
        "status": "ok",
        "open_positions": len(OPEN_POSITIONS),
        "account_equity": format_usdt(ACCOUNT_EQUITY_USDT)
    }

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
    # 💡 修正: position_monitor_schedulerはポジションを監視し続けるため、別のタスクとして実行
    asyncio.create_task(position_monitor_scheduler())


# エラーハンドラ 
@app.exception_handler(Exception)
async def default_exception_handler(request, exc):
    """捕捉されなかった例外を処理し、ログに記録する"""
    
    # CCXT接続終了時の警告などを無視
    if "Unclosed" not in str(exc):
        logging.error(f"❌ 未処理の致命的なエラーが発生しました: {exc}")
        
    return JSONResponse(
        status_code=500,
        content={"message": "Internal Server Error", "detail": str(exc)},
    )

if __name__ == "__main__":
    # 環境変数からポートを取得 (Renderデプロイ用)
    port = int(os.environ.get("PORT", 8000))
    # 💡 uvicornの起動 (render.comの実行コマンドと一致)
    uvicorn.run(app, host="0.0.0.0", port=port)
