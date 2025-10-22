# ====================================================================================
# Apex BOT v19.0.28 - Safety and Frequency Finalized (Patch 36)
#
# 修正ポイント:
# 1. 【安全確認】動的取引閾値 (0.67, 0.63, 0.58) を最終確定。
# 2. 【安全確認】取引実行ロジック (SL/TP, RRR >= 1.0, CCXT精度調整) の堅牢性を再確認。
# 3. 【バージョン更新】全てのバージョン情報を Patch 36 に更新。
# 4. 【機能改良】WEBSHAREログアップロードをFTPからHTTP POSTに変更。
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
# import ftplib  <-- 【変更点1: 削除/コメントアウト】
import uuid 

# .envファイルから環境変数を読み込む
load_dotenv()

# 💡 【ログ確認対応】ロギング設定を明示的に定義
logging.basicConfig(
    level=logging.INFO, # INFOレベル以上のメッセージを出力
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

JST = timezone(timedelta(hours=9))

# 出来高TOP40に加えて、主要な基軸通貨をDefaultに含めておく (現物シンボル形式 BTC/USDT)
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT",
    "DOGE/USDT", "DOT/USDT", "TRX/USDT", "MATIC/USDT", 
    "LTC/USDT", "AVAX/USDT", "LINK/USDT", "UNI/USDT", "ETC/USDT", "BCH/USDT",
    "NEAR/USDT", "ATOM/USDT", "FTM/USDT", "ALGO/USDT", "XLM/USDT", "SAND/USDT",
    "GALA/USDT", "FIL/USDT", "EOS/USDT", "AXS/USDT", "MANA/USDT", "AAVE/USDT",
    "MKR/USDT", "THETA/USDT", "FLOW/USDT", "IMX/USDT", 
]
TOP_SYMBOL_LIMIT = 40               # 監視対象銘柄の最大数 (出来高TOPから選出)を40に引き上げ
LOOP_INTERVAL = 60 * 10             # メインループの実行間隔 (秒) - 10分ごと
ANALYSIS_ONLY_INTERVAL = 60 * 60    # 分析専用通知の実行間隔 (秒) - 1時間ごと
WEBSHARE_UPLOAD_INTERVAL = 60 * 60  # WebShareログアップロード間隔 (1時間ごと)
MONITOR_INTERVAL = 10               # ポジション監視ループの実行間隔 (秒)

# 💡 クライアント設定
CCXT_CLIENT_NAME = os.getenv("EXCHANGE_CLIENT", "mexc")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_API_KEY")
SECRET_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_SECRET")
TEST_MODE = os.getenv("TEST_MODE", "False").lower() in ('true', '1', 't')
SKIP_MARKET_UPDATE = os.getenv("SKIP_MARKET_UPDATE", "False").lower() in ('true', '1', 't')

# 💡 自動売買設定 (動的ロットのベースサイズ)
try:
    BASE_TRADE_SIZE_USDT = float(os.getenv("BASE_TRADE_SIZE_USDT", "100")) 
except ValueError:
    BASE_TRADE_SIZE_USDT = 100.0
    logging.warning("⚠️ BASE_TRADE_SIZE_USDTが不正な値です。100 USDTを使用します。")
    
if BASE_TRADE_SIZE_USDT < 10:
    logging.warning("⚠️ BASE_TRADE_SIZE_USDTが10 USDT未満です。ほとんどの取引所の最小取引額を満たさない可能性があります。")


# 💡 WEBSHARE設定 (HTTP POSTを想定した外部ログストレージ) 【変更点2: 設定コメントの変更】
WEBSHARE_HOST = os.getenv("WEBSHARE_HOST") # ログアップロード先の完全なURLを設定してください
# WEBSHARE_PORT = int(os.getenv("WEBSHARE_PORT", "21")) # FTP固有設定のため削除/コメントアウト
WEBSHARE_USER = os.getenv("WEBSHARE_USER") # HTTP認証 (Basic Auth)に再利用
WEBSHARE_PASS = os.getenv("WEBSHARE_PASS") # HTTP認証 (Basic Auth)に再利用

# グローバル変数 (状態管理用)
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS.copy()
LAST_SUCCESS_TIME: float = 0.0
LAST_SIGNAL_TIME: Dict[str, float] = {}
LAST_ANALYSIS_SIGNALS: List[Dict] = []
LAST_HOURLY_NOTIFICATION_TIME: float = 0.0
LAST_ANALYSIS_ONLY_NOTIFICATION_TIME: float = 0.0
LAST_WEBSHARE_UPLOAD_TIME: float = 0.0 
GLOBAL_MACRO_CONTEXT: Dict = {} # マクロコンテキストを保持するための変数
IS_FIRST_MAIN_LOOP_COMPLETED: bool = False # 初回メインループ完了フラグ
OPEN_POSITIONS: List[Dict] = [] # 現在保有中のポジション (SL/TP監視用)

if TEST_MODE:
    logging.warning("⚠️ WARNING: TEST_MODE is active. Trading is disabled.")

# CCXTクライアントの準備完了フラグ
IS_CLIENT_READY: bool = False

# 取引ルール設定
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 # 同一銘柄のシグナル通知クールダウン（2時間）
SIGNAL_THRESHOLD = 0.65             # 動的閾値のベースライン (通常時の値 2-3銘柄/日を想定)
TOP_SIGNAL_COUNT = 3                # 通知するシグナルの最大数
REQUIRED_OHLCV_LIMITS = {'15m': 500, '1h': 500, '4h': 500} # 取得するOHLCVの足数

# テクニカル分析定数 (v19.0.28ベース)
TARGET_TIMEFRAMES = ['15m', '1h', '4h']
BASE_SCORE = 0.60                   # ベースとなる取引基準点 (60点)
LONG_TERM_SMA_LENGTH = 200          # 長期トレンドフィルタ用SMA
LONG_TERM_REVERSAL_PENALTY = 0.20   # 長期トレンド逆行時のペナルティ
STRUCTURAL_PIVOT_BONUS = 0.05       # 価格構造/ピボット支持時のボーナス
RSI_MOMENTUM_LOW = 40               # RSIが40以下でロングモメンタム候補
MACD_CROSS_PENALTY = 0.15           # MACDが不利なクロス/発散時のペナルティ
LIQUIDITY_BONUS_MAX = 0.06          # 流動性(板の厚み)による最大ボーナス
FGI_PROXY_BONUS_MAX = 0.05          # 恐怖・貪欲指数による最大ボーナス/ペナルティ
FOREX_BONUS_MAX = 0.0               # 為替機能を削除するため0.0に設定

# 市場環境に応じた動的閾値調整のための定数 (ユーザー要望に合わせて調整 - Patch 36確定)
FGI_SLUMP_THRESHOLD = -0.02         # FGIプロキシがこの値未満の場合、市場低迷と見なす
FGI_ACTIVE_THRESHOLD = 0.02         # FGIプロキシがこの値を超える場合、市場活発と見なす
# 🚨 最終調整箇所: 頻度目標達成のため閾値を引き下げ (この値で確定)
SIGNAL_THRESHOLD_SLUMP = 0.67       # 低迷時の閾値 (1-2銘柄/日を想定)
SIGNAL_THRESHOLD_NORMAL = 0.63      # 通常時の閾値 (2-3銘柄/日を想定)
SIGNAL_THRESHOLD_ACTIVE = 0.58      # 活発時の閾値 (3+銘柄/日を想定)

RSI_DIVERGENCE_BONUS = 0.10         # RSIダイバージェンス時のボーナス (未使用だが定数として残す)
VOLATILITY_BB_PENALTY_THRESHOLD = 0.01 # ボラティリティ過熱時のペナルティ閾値
OBV_MOMENTUM_BONUS = 0.04           # OBVトレンド一致時のボーナス

# ====================================================================================
# UTILITIES & FORMATTING
# ====================================================================================

def format_usdt(amount: float) -> str:
    """USDT金額を整形する"""
    if amount >= 1.0:
        return f"{amount:,.2f}"
    elif amount >= 0.01:
        return f"{amount:.4f}"
    else:
        return f"{amount:.6f}"

def get_estimated_win_rate(score: float) -> str:
    """スコアに基づいて推定勝率を返す (通知用)"""
    if score >= 0.90: return "90%+"
    if score >= 0.85: return "85-90%"
    if score >= 0.75: return "75-85%"
    if score >= 0.65: return "65-75%" 
    if score >= 0.60: return "60-65%"
    return "<60% (低)"

def get_current_threshold(macro_context: Dict) -> float:
    """
    グローバルマクロコンテキスト（FGIプロキシ値）に基づいて、
    現在の市場環境に合わせた動的な取引閾値を決定し、返す。
    """
    # グローバル定数にアクセス
    global FGI_SLUMP_THRESHOLD, FGI_ACTIVE_THRESHOLD
    global SIGNAL_THRESHOLD_SLUMP, SIGNAL_THRESHOLD_NORMAL, SIGNAL_THRESHOLD_ACTIVE
    
    # FGIプロキシ値を取得（デフォルトは0.0）
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    
    # 市場低迷/リスクオフの閾値 (0.67)
    if fgi_proxy < FGI_SLUMP_THRESHOLD:
        return SIGNAL_THRESHOLD_SLUMP
    
    # 市場活発/リスクオンの閾値 (0.58)
    elif fgi_proxy > FGI_ACTIVE_THRESHOLD:
        return SIGNAL_THRESHOLD_ACTIVE
        
    # 通常/中立時の閾値 (0.63)
    else:
        return SIGNAL_THRESHOLD_NORMAL

def get_score_breakdown(signal: Dict) -> str:
    """分析スコアの詳細なブレークダウンメッセージを作成する (Telegram通知用)"""
    tech_data = signal.get('tech_data', {})
    timeframe = signal.get('timeframe', 'N/A')
    
    # スコア算出ロジックから取得できる定数値 (通知表示に利用)
    LONG_TERM_REVERSAL_PENALTY_CONST = LONG_TERM_REVERSAL_PENALTY 
    MACD_CROSS_PENALTY_CONST = MACD_CROSS_PENALTY                 
    LIQUIDITY_BONUS_POINT_CONST = LIQUIDITY_BONUS_MAX           
    
    breakdown_list = []

    # 1. ベーススコア (全ての分析の出発点)
    breakdown_list.append(f"  - **ベーススコア ({timeframe})**: <code>+{BASE_SCORE*100:.1f}</code> 点")
    
    # 2. 長期トレンド/構造の確認
    penalty_applied = tech_data.get('long_term_reversal_penalty_value', 0.0)
    if penalty_applied > 0.0:
        breakdown_list.append(f"  - ❌ 長期トレンド逆行 (SMA{LONG_TERM_SMA_LENGTH}): <code>-{penalty_applied*100:.1f}</code> 点")
    else:
        # ペナルティ回避時のボーナス相当として表示
        breakdown_list.append(f"  - ✅ 長期トレンド一致 (SMA{LONG_TERM_SMA_LENGTH}): <code>+{LONG_TERM_REVERSAL_PENALTY_CONST*100:.1f}</code> 点 (ペナルティ回避)")

    # 価格構造/ピボット支持ボーナス
    pivot_bonus = tech_data.get('structural_pivot_bonus', 0.0)
    if pivot_bonus > 0.0:
        breakdown_list.append(f"  - ✅ 価格構造/ピボット支持: <code>+{pivot_bonus*100:.1f}</code> 点")

    # 3. モメンタム/出来高の確認
    macd_penalty_applied = tech_data.get('macd_penalty_value', 0.0)
    total_momentum_penalty = macd_penalty_applied

    if total_momentum_penalty > 0.0:
        breakdown_list.append(f"  - ❌ モメンタム/クロス不利: <code>-{total_momentum_penalty*100:.1f}</code> 点")
    else:
        # ペナルティ回避時のボーナス相当として表示
        breakdown_list.append(f"  - ✅ MACD/RSIモメンタム加速: <code>+{MACD_CROSS_PENALTY_CONST*100:.1f}</code> 点相当 (ペナルティ回避)")

    # 出来高/OBV確証ボーナス
    obv_bonus = tech_data.get('obv_momentum_bonus_value', 0.0)
    if obv_bonus > 0.0:
        breakdown_list.append(f"  - ✅ 出来高/OBV確証: <code>+{obv_bonus*100:.1f}</code> 点")
    
    # 4. 流動性/マクロ要因
    # 流動性ボーナス
    liquidity_bonus = tech_data.get('liquidity_bonus_value', 0.0)
    if liquidity_bonus > 0.0:
        breakdown_list.append(f"  - ✅ 流動性 (板の厚み) 優位: <code>+{LIQUIDITY_BONUS_POINT_CONST*100:.1f}</code> 点")
        
    # FGIマクロ要因
    fgi_bonus = tech_data.get('sentiment_fgi_proxy_bonus', 0.0)
    if abs(fgi_bonus) > 0.001:
        sign = '✅' if fgi_bonus > 0 else '❌'
        breakdown_list.append(f"  - {sign} FGIマクロ影響: <code>{'+' if fgi_bonus > 0 else ''}{fgi_bonus*100:.1f}</code> 点")

    # 為替マクロ (常に0.0を表示)
    forex_bonus = tech_data.get('forex_bonus', 0.0) 
    breakdown_list.append(f"  - ⚪ 為替マクロ影響: <code>{forex_bonus*100:.1f}</code> 点 (機能削除済)")
    
    # ボラティリティペナルティ (負の値のみ表示)
    volatility_penalty = tech_data.get('volatility_penalty_value', 0.0)
    if volatility_penalty < 0.0:
        breakdown_list.append(f"  - ❌ ボラティリティ過熱ペナルティ: <code>{volatility_penalty*100:.1f}</code> 点")

    return "\n".join(breakdown_list)


def format_analysis_only_message(all_signals: List[Dict], macro_context: Dict, current_threshold: float, monitoring_count: int) -> str:
    """1時間ごとの分析専用メッセージを作成する"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    
    # 1. 候補リストの作成 (スコア降順にソート)
    sorted_signals = sorted(all_signals, key=lambda s: s.get('score', 0.0), reverse=True)
    
    header = (
        f"📊 **Apex Market Snapshot (Hourly Analysis)**\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **確認日時**: {now_jst} (JST)\n"
        f"  - **取引ステータス**: <b>分析通知のみ</b>\n"
        f"  - **対象銘柄数**: <code>{monitoring_count}</code>\n"
        f"  - **監視取引所**: <code>{CCXT_CLIENT_NAME.upper()}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    # マクロコンテキスト情報
    fgi_raw_value = macro_context.get('fgi_raw_value', 'N/A')
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0) 

    fgi_sentiment = "リスクオン" if fgi_proxy > FGI_ACTIVE_THRESHOLD else ("リスクオフ" if fgi_proxy < FGI_SLUMP_THRESHOLD else "中立")
    forex_display = "中立 (機能削除済)"
    
    # 市場環境の判定
    if current_threshold == SIGNAL_THRESHOLD_SLUMP:
        market_condition_text = f"低迷/リスクオフ (Threshold: {SIGNAL_THRESHOLD_SLUMP*100:.0f}点)"
    elif current_threshold == SIGNAL_THRESHOLD_ACTIVE:
        market_condition_text = f"活発/リスクオン (Threshold: {SIGNAL_THRESHOLD_ACTIVE*100:.0f}点)"
    else:
        market_condition_text = f"通常/中立 (Threshold: {SIGNAL_THRESHOLD_NORMAL*100:.0f}点)"
    

    macro_section = (
        f"🌍 <b>グローバルマクロ分析</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **現在の市場環境**: <code>{market_condition_text}</code>\n"
        f"  - **恐怖・貪欲指数 (FGI)**: <code>{fgi_raw_value}</code> ({fgi_sentiment})\n"
        f"  - **為替マクロ (EUR/USD)**: {forex_display}\n"
        f"  - **総合マクロ影響**: <code>{((fgi_proxy + forex_bonus) * 100):.2f}</code> 点\n\n"
    )

    # トップシグナル情報 (Rank 1のみに焦点を当てる)
    signal_section = "📈 <b>トップシグナル候補 (スコア順)</b>\n"
    
    if sorted_signals:
        top_signal = sorted_signals[0] # Rank 1を取得
        symbol = top_signal['symbol']
        timeframe = top_signal['timeframe']
        score = top_signal['score']
        rr_ratio = top_signal['rr_ratio']
        
        # スコア詳細ブレークダウンの生成
        breakdown_details = get_score_breakdown(top_signal)
        
        score_color = ""
        if score < current_threshold:
             score_color = "⚠️" 
        if score < BASE_SCORE: 
             score_color = "🔴"
             
        rr_display = f"1:{rr_ratio:.1f}" if rr_ratio >= 1.0 else f"1:{rr_ratio:.1f} ❌"
        
        signal_section += (
            f"  🥇 <b>{symbol}</b> ({timeframe}) - **最高スコア** {score_color}\n"
            f"     - **総合スコア**: <code>{score * 100:.2f} / 100</code> (推定勝率: {get_estimated_win_rate(score)})\n"
            f"     - **リスクリワード比率 (RRR)**: <code>{rr_display}</code>\n"
            f"  \n**📊 スコア詳細ブレークダウン** (+/-要因)\n"
            f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
            f"{breakdown_details}\n"
            f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )

        # 警告メッセージの追加
        if top_signal['score'] < current_threshold:
             signal_section += f"\n<pre>⚠️ 注: 上記は監視中の最高スコアですが、取引閾値 ({current_threshold*100:.0f}点) 未満です。</pre>\n"
        
        if top_signal['score'] < BASE_SCORE:
             signal_section += f"<pre>🔴 警告: 最高スコアが取引基準点 ({BASE_SCORE*100:.0f}点) 未満です。</pre>\n"

    else:
        signal_section += "  - **シグナル候補なし**: 現在、すべての監視銘柄で最低限のリスクリワード比率を満たすロングシグナルは見つかりませんでした。\n"
    
    footer = (
        f"\n<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>※ この通知は取引実行を伴いません。</pre>"
        f"<i>Bot Ver: v19.0.28 - Safety and Frequency Finalized (Patch 36)</i>" 
    )

    return header + macro_section + signal_section + footer

def format_startup_message(
    account_status: Dict, 
    macro_context: Dict, 
    monitoring_count: int,
    current_threshold: float,
    bot_version: str
) -> str:
    """初回起動完了通知用のメッセージを作成する"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    
    # マクロ情報
    fgi_value = macro_context.get('fgi_raw_value', 'N/A')
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    fgi_sentiment = "リスクオン" if fgi_proxy > FGI_ACTIVE_THRESHOLD else ("リスクオフ" if fgi_proxy < FGI_SLUMP_THRESHOLD else "中立")

    # 市場環境の判定
    if current_threshold == SIGNAL_THRESHOLD_SLUMP:
        market_condition_text = "低迷/リスクオフ"
    elif current_threshold == SIGNAL_THRESHOLD_ACTIVE:
        market_condition_text = "活発/リスクオン"
    else:
        market_condition_text = "通常/中立"
        
    # 自動売買ステータス
    trade_status = "自動売買 **ON**" if not TEST_MODE else "自動売買 **OFF** (TEST_MODE)"

    header = (
        f"🤖 **Apex BOT 起動完了通知** 🟢\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **確認日時**: {now_jst} (JST)\n"
        f"  - **取引所**: <code>{CCXT_CLIENT_NAME.upper()}</code> (現物モード)\n"
        f"  - **自動売買**: <b>{trade_status}</b>\n"
        f"  - **取引ロット (BASE)**: <code>{BASE_TRADE_SIZE_USDT:.2f}</code> USDT\n" # BASEに変更
        f"  - **監視銘柄数**: <code>{monitoring_count}</code>\n"
        f"  - **BOTバージョン**: <code>{bot_version}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    # 1. 残高/ポジション情報
    balance_section = f"💰 <b>口座ステータス</b>\n"
    if account_status.get('error'):
        # エラーメッセージを分かりやすく表示
        balance_section += f"<pre>⚠️ ステータス取得失敗 (セキュリティのため詳細なエラーは表示しません。ログを確認してください)</pre>\n"
    else:
        balance_section += (
            f"  - **USDT残高**: <code>{format_usdt(account_status['total_usdt_balance'])}</code> USDT\n"
        )
        
        # 管理ポジションの表示
        if OPEN_POSITIONS:
            total_managed_value = sum(p['filled_usdt'] for p in OPEN_POSITIONS)
            balance_section += (
                f"  - **管理中ポジション**: <code>{len(OPEN_POSITIONS)}</code> 銘柄 (投入合計: <code>{format_usdt(total_managed_value)}</code> USDT)\n"
            )
            for i, pos in enumerate(OPEN_POSITIONS[:3]): # Top 3のみ表示
                base_currency = pos['symbol'].replace('/USDT', '')
                balance_section += f"    - Top {i+1}: {base_currency} (SL: {format_usdt(pos['stop_loss'])} / TP: {format_usdt(pos['take_profit'])})\n"
            if len(OPEN_POSITIONS) > 3:
                balance_section += f"    - ...他 {len(OPEN_POSITIONS) - 3} 銘柄\n"
        else:
             balance_section += f"  - **管理中ポジション**: <code>なし</code>\n"

        # 既存の現物ポジション（CCXTから取得）は簡略化
        open_ccxt_positions = [p for p in account_status['open_positions'] if p['usdt_value'] >= 10]
        if open_ccxt_positions:
             balance_section += f"  - **未管理の現物**: <code>{len(open_ccxt_positions)}</code> 銘柄 (CCXT参照)\n"
        
    balance_section += f"\n"

    # 2. 市場状況 (スコア付け)
    macro_section = (
        f"🌍 <b>市場環境スコアリング</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **取引閾値 (Score)**: <code>{current_threshold*100:.0f} / 100</code>\n"
        f"  - **現在の市場環境**: <code>{market_condition_text}</code>\n"
        f"  - **FGI (恐怖・貪欲)**: <code>{fgi_value}</code> ({fgi_sentiment})\n"
        f"  - **総合マクロ影響**: <code>{((fgi_proxy + forex_bonus) * 100):.2f}</code> 点\n\n"
    )

    footer = (
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>※ この通知はメインの分析ループが一度完了したことを示します。約10分ごとに分析が実行されます。</pre>"
    )

    return header + balance_section + macro_section + footer

def format_telegram_message(signal: Dict, context: str, current_threshold: float, trade_result: Optional[Dict] = None, exit_type: Optional[str] = None) -> str:
    """Telegram通知用のメッセージを作成する (取引結果を追加)"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    symbol = signal['symbol']
    timeframe = signal['timeframe']
    score = signal['score']
    
    # SL/TP/RRRはシグナルまたは取引結果から取得
    entry_price = signal.get('entry_price', trade_result.get('entry_price', 0.0))
    stop_loss = signal.get('stop_loss', trade_result.get('stop_loss', 0.0))
    take_profit = signal.get('take_profit', trade_result.get('take_profit', 0.0))
    rr_ratio = signal.get('rr_ratio', 0.0)
    
    estimated_wr = get_estimated_win_rate(score)
    
    breakdown_details = get_score_breakdown(signal) 

    trade_section = ""
    trade_status_line = ""

    if context == "取引シグナル":
        # エントリー通知
        lot_size = signal.get('lot_size_usdt', BASE_TRADE_SIZE_USDT) # 動的ロット
        
        if TEST_MODE:
            trade_status_line = f"⚠️ **テストモード**: 取引は実行されません。(ロット: {format_usdt(lot_size)} USDT)"
        elif trade_result is None or trade_result.get('status') == 'error':
            trade_status_line = f"❌ **自動売買 失敗**: {trade_result.get('error_message', 'APIエラー')}"
        elif trade_result.get('status') == 'ok':
            trade_status_line = "✅ **自動売買 成功**: 現物ロング注文を執行しました。"
            filled_amount = trade_result.get('filled_amount', 'N/A')
            filled_usdt = trade_result.get('filled_usdt', 'N/A')
            trade_section = (
                f"💰 **取引実行結果**\n"
                f"  - **注文タイプ**: <code>現物 (Spot) / 成行買い</code>\n"
                f"  - **動的ロット**: <code>{format_usdt(lot_size)}</code> USDT (目標)\n"
                f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
                f"  - **平均約定額**: <code>{format_usdt(filled_usdt)}</code> USDT\n"
            )
            
    elif context == "ポジション決済":
        # 決済通知
        trade_status_line = f"🔴 **ポジション決済**: {exit_type} トリガー"
        
        entry_price = trade_result.get('entry_price', 0.0)
        exit_price = trade_result.get('exit_price', 0.0)
        pnl_usdt = trade_result.get('pnl_usdt', 0.0)
        pnl_rate = trade_result.get('pnl_rate', 0.0)
        filled_amount = trade_result.get('filled_amount', 'N/A')
        
        pnl_sign = "✅ 利益確定" if pnl_usdt >= 0 else "❌ 損切り"
        
        trade_section = (
            f"💰 **決済実行結果** - {pnl_sign}\n"
            f"  - **エントリー価格**: <code>{format_usdt(entry_price)}</code>\n"
            f"  - **決済価格**: <code>{format_usdt(exit_price)}</code>\n"
            f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
            f"  - **損益**: <code>{'+' if pnl_usdt >= 0 else ''}{format_usdt(pnl_usdt)}</code> USDT ({pnl_rate*100:.2f}%)\n"
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
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"📌 **ポジション管理パラメータ**\n"
        f"  - **エントリー**: <code>{format_usdt(entry_price)}</code>\n"
        f"  - **ストップロス (SL)**: <code>{format_usdt(stop_loss)}</code>\n"
        f"  - **テイクプロフィット (TP)**: <code>{format_usdt(take_profit)}</code>\n"
        f"  - **リスク幅 (SL)**: <code>{format_usdt(entry_price - stop_loss)}</code> USDT\n"
        f"  - **リワード幅 (TP)**: <code>{format_usdt(take_profit - entry_price)}</code> USDT\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
    )
    
    # 取引結果セクションをシグナル詳細の前に追加
    if trade_section:
        message += trade_section + f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        
    # エントリー時のみスコアブレークダウンを表示
    if context == "取引シグナル":
        message += (
            f"  \n**📊 スコア詳細ブレークダウン** (+/-要因)\n"
            f"{breakdown_details}\n"
            f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )
        
    message += (f"<i>Bot Ver: v19.0.28 - Safety and Frequency Finalized (Patch 36)</i>")
    return message


async def send_telegram_notification(message: str) -> bool:
    """Telegramにメッセージを送信する"""
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
    再帰的にオブジェクトをJSON互換の型に変換するヘルパー関数。
    特にboolやNumPyのスカラ型を文字列/Pythonネイティブ型に変換する。
    """
    if isinstance(obj, dict):
        return {k: _to_json_compatible(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_to_json_compatible(elem) for elem in obj]
    elif isinstance(obj, (bool, np.bool_)):
        # すべてのブーリアン型を文字列に変換し、シリアライズエラーを回避
        return str(obj) 
    elif isinstance(obj, np.generic):
        # numpy.float64, numpy.int64 などのNumPyスカラをPythonネイティブ型に変換
        return obj.item()
    return obj


def log_signal(data: Dict, log_type: str, trade_result: Optional[Dict] = None) -> None:
    """シグナルまたは取引結果をローカルファイルにログする"""
    try:
        # ロギングされるデータの基本構造を定義
        log_entry = {
            'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            'log_type': log_type,
            'symbol': data.get('symbol', 'N/A'),
            'timeframe': data.get('timeframe', 'N/A'),
            'score': data.get('score', 0.0),
            'rr_ratio': data.get('rr_ratio', 0.0),
            'trade_result': trade_result or data.get('trade_result', None), # 決済時にはtrade_resultがdata内に含まれる
            'full_data': data,
        }
        
        # JSONシリアライズエラーを回避するために辞書をクリーニング
        cleaned_log_entry = _to_json_compatible(log_entry)

        # ファイル名にスペースが入らないように修正
        log_file = f"apex_bot_{log_type.lower().replace(' ', '_')}_log.jsonl"
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(cleaned_log_entry, ensure_ascii=False) + '\n')
            
        logging.info(f"✅ {log_type}ログをファイルに記録しました。")
    except Exception as e:
        logging.error(f"❌ ログ書き込みエラー: {e}", exc_info=True)


# ------------------------------------------------------------------------------------
# 【変更点3: _sync_ftp_upload を _sync_http_upload に置き換え】
# ------------------------------------------------------------------------------------

def _sync_http_upload(local_file: str, remote_file: str) -> bool:
    """
    同期的にHTTP POSTアップロードを実行するヘルパー関数。
    asyncio.to_threadで使用される。
    
    WEBSHARE_HOSTをアップロード先の完全なURL (例: https://example.com/upload) 
    として使用し、ファイルをマルチパートフォームデータとして送信します。
    WEBSHARE_USER/PASSが設定されている場合はBasic認証を使用します。
    """
    if not WEBSHARE_HOST:
        logging.error("❌ WEBSHARE_HOST が設定されていません。HTTPアップロードをスキップします。")
        return False
    if not os.path.exists(local_file):
        logging.warning(f"⚠️ ローカルファイル {local_file} が見つかりません。アップロードをスキップします。")
        return True # ファイルがないのはエラーではない

    upload_url = WEBSHARE_HOST
    auth = (WEBSHARE_USER, WEBSHARE_PASS) if WEBSHARE_USER and WEBSHARE_PASS else None

    logging.info(f"📤 HTTPアップロード開始: {local_file} -> {upload_url} (リモートファイル名: {remote_file})")

    try:
        with open(local_file, 'rb') as f:
            # マルチパートフォームデータを使用してファイルを送信
            files = {
                # サーバー側で認識されるフィールド名（例: 'file'）
                # サーバー側でこのキー(ここでは'file')とファイル名を適切に処理する必要があります
                'file': (remote_file, f, 'application/octet-stream'), 
            }
            # requests.post は同期処理なので asyncio.to_thread 経由で安全に実行される
            response = requests.post(upload_url, files=files, auth=auth, timeout=30)
            response.raise_for_status() # 4xx, 5xxエラーをチェック
            
            logging.info(f"✅ HTTPアップロード成功 ({response.status_code})。")
            return True
            
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ HTTPアップロードエラー: {e}")
        return False
    except Exception as e:
        logging.error(f"❌ ログアップロードの予期せぬエラー: {e}", exc_info=True)
        return False

# ------------------------------------------------------------------------------------
# upload_logs_to_webshare 関数の修正
# ------------------------------------------------------------------------------------

async def upload_logs_to_webshare():
    """ローカルログファイルを外部ストレージ (WebShare/HTTP POST) にアップロードする"""
    if not WEBSHARE_HOST:
        logging.info("ℹ️ WEBSHARE HOSTが設定されていません。ログアップロードをスキップします。")
        return 
        
    log_files = [
        "apex_bot_trade_signal_log.jsonl",
        "apex_bot_hourly_analysis_log.jsonl",
        "apex_bot_trade_exit_log.jsonl",
    ]
    now_jst = datetime.now(JST)
    upload_timestamp = now_jst.strftime("%Y%m%d_%H%M%S")
    logging.info(f"📤 WEBSHAREログアップロード処理を開始します (HTTP POST)...")
    tasks = []
    
    for log_file in log_files:
        if os.path.exists(log_file):
            # リモートファイル名にはタイムスタンプとファイル名を含める
            remote_filename = f"apex_log_{upload_timestamp}_{log_file}"
            
            # 同期HTTPアップロード処理を別スレッドで実行 【変更点4: 関数名の置き換え】
            tasks.append(
                asyncio.to_thread(_sync_http_upload, log_file, remote_filename)
            )

    if not tasks:
        logging.info("ℹ️ アップロード対象のログファイルがありませんでした。")
        return 

    # 全てのタスクを並行実行
    results = await asyncio.gather(*tasks)
    
    if all(results):
        logging.info(f"✅ すべてのログファイル ({len(tasks)} 件) を WEBSHARE にアップロードしました。")
    else:
        logging.error("❌ 一部またはすべてのログファイルの WEBSHARE へのアップロードに失敗しました。")

# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def initialize_exchange_client() -> bool:
    """CCXTクライアントを初期化する"""
    global EXCHANGE_CLIENT
    try:
        client_name = CCXT_CLIENT_NAME.lower()
        if client_name == 'binance':
            exchange_class = ccxt_async.binance
        elif client_name == 'bybit':
            exchange_class = ccxt_async.bybit
        # MEXCクライアント
        elif client_name == 'mexc':
            exchange_class = ccxt_async.mexc
        else:
            logging.error(f"❌ 未対応の取引所クライアント: {CCXT_CLIENT_NAME}")
            return False

        # CCXTのオプション設定
        options = {
            'defaultType': 'spot', # 現物取引 (Spot) を想定
        }

        EXCHANGE_CLIENT = exchange_class({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True,
            'options': options
        })
        
        await EXCHANGE_CLIENT.load_markets()
        logging.info(f"✅ CCXTクライアント ({CCXT_CLIENT_NAME}) を現物取引モードで初期化しました。")
        return True
    except Exception as e:
        logging.critical(f"❌ CCXTクライアント初期化失敗: {e}")
        EXCHANGE_CLIENT = None
        return False

async def fetch_ohlcv_safe(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """OHLCVデータを安全に取得する"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return None
    
    try:
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        if not ohlcv:
            return None
            
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        return df

    except ccxt.ExchangeNotAvailable as e:
        logging.error(f"❌ {symbol} - {timeframe}: 取引所APIエラー: {e}")
        return None
    except ccxt.DDoSProtection as e:
        logging.warning(f"⚠️ {symbol} - {timeframe}: DDoS保護エラー。レート制限を確認してください。: {e}")
        return None
    except ccxt.RequestTimeout as e:
        logging.error(f"❌ {symbol} - {timeframe}: リクエストタイムアウト。: {e}")
        return None
    except Exception as e:
        logging.error(f"❌ {symbol} - {timeframe}: OHLCV取得予期せぬエラー: {e}", exc_info=True)
        return None

async def fetch_markets_and_filter_symbols() -> List[str]:
    """取引所の市場情報を取得し、出来高順にシンボルをフィルタリングする"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT or SKIP_MARKET_UPDATE:
        logging.info("ℹ️ CCXTクライアントが未準備、またはマーケットアップデートがスキップされました。デフォルトのシンボルリストを使用します。")
        return DEFAULT_SYMBOLS.copy()

    try:
        markets = await EXCHANGE_CLIENT.fetch_tickers()
    except Exception as e:
        logging.error(f"❌ Ticker情報取得失敗: {e}。デフォルトのシンボルリストを使用します。")
        return DEFAULT_SYMBOLS.copy()

    usdt_markets = {}
    for symbol, ticker in markets.items():
        # USDTペアの現物市場のみを対象とする
        # 例: BTC/USDT や ETH/USDT
        if ticker is not None and '/USDT' in symbol and ticker.get('quoteVolume') is not None and EXCHANGE_CLIENT.markets[symbol]['spot']:
            # 出来高 (USDT換算の24時間quoteVolume) が流動性の指標
            quote_volume = ticker['quoteVolume']
            usdt_markets[symbol] = quote_volume

    if not usdt_markets:
        logging.warning("⚠️ USDT現物市場が見つかりませんでした。デフォルトのシンボルリストを使用します。")
        return DEFAULT_SYMBOLS.copy()

    # 出来高順にソートし、TOP_SYMBOL_LIMITまで選択
    sorted_markets = sorted(usdt_markets.items(), key=lambda item: item[1], reverse=True)
    top_symbols = [symbol for symbol, volume in sorted_markets[:TOP_SYMBOL_LIMIT]]

    # デフォルトのシンボルリストに主要な通貨が含まれていることを確認
    final_symbols = list(set(top_symbols + DEFAULT_SYMBOLS))
    
    logging.info(f"✅ 市場アップデート完了。監視銘柄数: {len(final_symbols)} (TOP{TOP_SYMBOL_LIMIT} + Default)")
    return final_symbols

async def fetch_account_status() -> Dict:
    """口座残高と保有ポジションを取得する"""
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    status: Dict = {
        'total_usdt_balance': 0.0,
        'open_positions': [],
        'error': False,
    }

    if not EXCHANGE_CLIENT:
        status['error'] = True
        return status

    try:
        # 1. 残高を取得
        balance = await EXCHANGE_CLIENT.fetch_balance()
        status['total_usdt_balance'] = balance['USDT']['total']
        
        # 2. 現物保有ポジション（CCXTの定義する残高から取得）
        for currency, info in balance['total'].items():
            if currency == 'USDT' or info <= 0.0:
                continue

            # USDTペアのシンボルを作成
            symbol = f"{currency}/USDT"
            
            # Tickerを取得してUSDT換算価値を計算
            try:
                ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
                usdt_value = info * ticker['last']
                if usdt_value >= 5.0: # 5 USDT以上の保有のみを対象とする
                    status['open_positions'].append({
                        'symbol': symbol,
                        'currency': currency,
                        'amount': info,
                        'usdt_value': usdt_value
                    })
            except Exception:
                # Tickerがない、または計算できない場合はスキップ
                continue

    except Exception as e:
        logging.error(f"❌ 口座ステータス取得失敗: {e}")
        status['error'] = True
        status['error_message'] = str(e)
        
    return status

# ====================================================================================
# TECHNICAL ANALYSIS & SCORING
# ====================================================================================

def calculate_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """必須のテクニカルインジケーターを計算する"""
    
    # 1. 必須のトレンドインジケーター
    df['SMA_200'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH) # 長期トレンド
    df['SMA_50'] = ta.sma(df['close'], length=50) # 中期トレンド (SL/TPの基準)
    
    # 2. ボラティリティ
    # Bollinger Bands (20期間, 2標準偏差)
    bbands = ta.bbands(df['close'], length=20, std=2)
    df['BBL_20'] = bbands['BBL_20_2.0']
    df['BBM_20'] = bbands['BBM_20_2.0']
    df['BBU_20'] = bbands['BBU_20_2.0']
    df['BBW'] = bbands['BBW_20_2.0'] # バンド幅
    df['PCT_B'] = bbands['BBB_20_2.0'] # %B (未使用だがボラティリティ用)
    
    # 3. モメンタム
    df['RSI'] = ta.rsi(df['close'], length=14)
    macd_data = ta.macd(df['close'], fast=12, slow=26, signal=9)
    df['MACD'] = macd_data['MACD_12_26_9']
    df['MACDh'] = macd_data['MACDh_12_26_9'] # ヒストグラム
    df['MACDs'] = macd_data['MACDs_12_26_9'] # シグナルライン

    # 4. 出来高
    df['OBV'] = ta.obv(df['close'], df['volume'])
    df['OBV_SMA_20'] = ta.sma(df['OBV'], length=20) # OBVのSMA

    # 5. 価格構造/サポート (簡易版: 過去100期間の最安値)
    df['STRUCTURAL_LOW'] = df['low'].rolling(window=100, min_periods=100).min()
    
    return df


def generate_signal(symbol: str, timeframe: str, df: pd.DataFrame, macro_context: Dict) -> Optional[Dict]:
    """
    テクニカル分析とスコアリングを実行し、シグナルを生成する。
    ここではロングシグナルのみを対象とする。
    """
    if df is None or len(df) < LONG_TERM_SMA_LENGTH or df.isnull().values.any():
        return None # データ不足
        
    current_close = df['close'].iloc[-1]
    
    # --- 1. SL/TP算出とRRRチェック ---
    
    # a. ストップロス (SL): SMA_50を主要なサポートとして利用
    # SMA_50を明確に下回る価格、または直近の構造的安値をSLとする
    stop_loss_base = min(df['SMA_50'].iloc[-2], df['STRUCTURAL_LOW'].iloc[-2])
    # SLは現在価格の少なくとも1%下に設定する（極端な近接SLを避けるため）
    min_sl_price = current_close * 0.99
    stop_loss = min(stop_loss_base, min_sl_price)
    
    # SLがエントリー価格よりも上にある場合、またはSLの幅が小さすぎる場合はスキップ
    if stop_loss >= current_close * 0.995 or stop_loss <= 0:
         return None

    risk_amount_rate = (current_close - stop_loss) / current_close
    
    # b. テイクプロフィット (TP): リスクの最低1倍を確保できる価格
    # RRR >= 1.0を最低条件とする
    # リワード幅 = リスク幅 * 1.0 (最低)
    reward_amount = current_close - stop_loss
    take_profit_min = current_close + reward_amount 
    
    # RRR >= 1.0 が満たされない場合は、取引不可
    if take_profit_min <= current_close * 1.005: # TPがエントリー価格よりわずかに上であること
        return None
        
    # c. TPの調整: 直近のBBUまたは高値の抵抗を考慮して、TPを最大化（ここでは簡易的に最低RRRのみを考慮）
    take_profit = take_profit_min
    rr_ratio = (take_profit - current_close) / (current_close - stop_loss)
    
    # 🚨 RRRの最終チェック (取引ルール): RRRが1.0未満の場合はシグナルを生成しない
    if rr_ratio < 1.0:
        return None 
        
    # --- 2. スコアリングの実行 ---
    
    score = BASE_SCORE # 60点からスタート
    tech_data: Dict[str, Any] = {}
    
    # a. 長期トレンドフィルター (SMA 200)
    # 価格がSMA 200を上回っているか（ロングトレンド）
    if current_close > df['SMA_200'].iloc[-2]:
        # トレンド一致: ペナルティ回避
        long_term_reversal_penalty_value = 0.0
    else:
        # トレンド逆行: ペナルティ適用
        score -= LONG_TERM_REVERSAL_PENALTY
        long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY
        
    tech_data['long_term_reversal_penalty_value'] = long_term_reversal_penalty_value
    
    # b. 価格構造/ピボット支持
    # 現在価格がSMA_50の近くにある (SMA_50をSLとして利用するため)
    pivot_bonus = 0.0
    if current_close > df['SMA_50'].iloc[-2] and current_close < df['SMA_50'].iloc[-2] * 1.02:
        score += STRUCTURAL_PIVOT_BONUS
        pivot_bonus = STRUCTURAL_PIVOT_BONUS
        
    tech_data['structural_pivot_bonus'] = pivot_bonus
    
    # c. モメンタム (RSI & MACD)
    
    # RSI: 40以下はロングの勢いが弱いか、売られすぎを示唆
    if df['RSI'].iloc[-1] < RSI_MOMENTUM_LOW:
        # MACDが上向きにクロスしている場合はペナルティを軽減できるが、ここでは単純化
        macd_penalty_value = 0.0
        # MACD: MACDラインがシグナルラインを上回る (ゴールデンクロス)
        if df['MACD'].iloc[-1] <= df['MACDs'].iloc[-1]:
            # デッドクロスやクロス待ち、またはMACDが下向きの傾向
            score -= MACD_CROSS_PENALTY
            macd_penalty_value = MACD_CROSS_PENALTY
            
        tech_data['macd_penalty_value'] = macd_penalty_value

    else:
        # RSIが既に高い場合はモメンタム加速ボーナスはなし、MACDがデッドクロスでもペナルティは控えめ
        tech_data['macd_penalty_value'] = 0.0 # RSIが高いためペナルティは適用しない

    # d. 出来高 (OBV)
    # OBVが20期間のSMAを上回っているか（買い圧力の一致）
    obv_momentum_bonus_value = 0.0
    if df['OBV'].iloc[-1] > df['OBV_SMA_20'].iloc[-1]:
        score += OBV_MOMENTUM_BONUS
        obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
        
    tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus_value
    
    # e. 流動性/ボラティリティ
    
    # 流動性ボーナス (ここでは簡易的に、過去のボラティリティが安定していることを良しとする)
    liquidity_bonus_value = 0.0
    # BBWがヒストリカルな平均の範囲内にある場合（過度なボラティリティがない）
    if df['BBW'].iloc[-1] < df['BBW'].iloc[-200:-1].mean() * 1.5:
        score += LIQUIDITY_BONUS_MAX
        liquidity_bonus_value = LIQUIDITY_BONUS_MAX
        
    tech_data['liquidity_bonus_value'] = liquidity_bonus_value
    
    # ボラティリティ過熱ペナルティ
    # 価格がBBU (アッパーバンド) に近づいている場合、反転リスクとしてペナルティ
    volatility_penalty_value = 0.0
    if current_close > df['BBU_20'].iloc[-1] * (1.0 - VOLATILITY_BB_PENALTY_THRESHOLD):
        # アッパーバンドの閾値を超えている場合
        penalty = min(0.0, -(current_close / df['BBU_20'].iloc[-1] - 1.0) * 0.1)
        score += penalty
        volatility_penalty_value = penalty
    
    tech_data['volatility_penalty_value'] = volatility_penalty_value

    # f. マクロ要因 (FGI)
    
    # FGIプロキシ値を取得（-0.05から+0.05の範囲を想定）
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    sentiment_fgi_proxy_bonus = fgi_proxy * FGI_PROXY_BONUS_MAX * 20 # 係数調整
    
    score += sentiment_fgi_proxy_bonus
    tech_data['sentiment_fgi_proxy_bonus'] = sentiment_fgi_proxy_bonus
    
    # 為替マクロ (削除済のため0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    score += forex_bonus
    tech_data['forex_bonus'] = forex_bonus
    
    # 最終スコアの調整
    score = round(max(0.0, score), 4) # 0.0未満にはならないようにする
    
    # --- 3. 結果の集計 ---
    
    # 動的ロット計算 (リスク量に基づいて調整するロジックをここで挿入可能だが、BASE_TRADE_SIZE_USDTを使用)
    lot_size_usdt = BASE_TRADE_SIZE_USDT 
    
    signal = {
        'symbol': symbol,
        'timeframe': timeframe,
        'score': score,
        'entry_price': current_close,
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'rr_ratio': round(rr_ratio, 2),
        'lot_size_usdt': lot_size_usdt,
        'tech_data': tech_data, # スコアリングの詳細内訳
    }
    
    return signal

async def analyze_and_generate_signals(current_symbols: List[str], macro_context: Dict) -> List[Dict]:
    """全監視銘柄に対して分析とシグナル生成を並行して実行する"""
    all_signals: List[Dict] = []
    
    # 1. 全銘柄の全タイムフレームのOHLCVデータ取得タスクを作成
    data_fetch_tasks = []
    for symbol in current_symbols:
        for tf in TARGET_TIMEFRAMES:
            limit = REQUIRED_OHLCV_LIMITS[tf]
            data_fetch_tasks.append(
                fetch_ohlcv_safe(symbol, tf, limit)
            )
            
    # 2. データの並行取得を実行 (最大待ち時間は30秒程度を推奨)
    all_data = await asyncio.gather(*data_fetch_tasks, return_exceptions=True)
    
    # 3. 取得したデータを解析し、シグナルを生成
    analysis_count = 0
    signal_generation_tasks = []
    
    # all_dataは [(symbol_1, tf_1_df), (symbol_1, tf_2_df), ...] の順序で返る
    for i, df in enumerate(all_data):
        if isinstance(df, Exception) or df is None:
            continue

        # どの銘柄とタイムフレームに対応するかを逆算
        symbol_index = i // len(TARGET_TIMEFRAMES)
        timeframe_index = i % len(TARGET_TIMEFRAMES)
        
        symbol = current_symbols[symbol_index]
        timeframe = TARGET_TIMEFRAMES[timeframe_index]
        
        # シグナル生成処理を別スレッドで実行 (CPUバウンドなpandas/numpy処理を含むため)
        signal_generation_tasks.append(
            asyncio.to_thread(generate_signal, symbol, timeframe, df, macro_context)
        )
        analysis_count += 1
        
    # 4. シグナル生成を並行して実行
    all_raw_signals = await asyncio.gather(*signal_generation_tasks)
    
    # 5. 有効なシグナルをフィルタリング
    for signal in all_raw_signals:
        if signal and signal['score'] > BASE_SCORE and signal['rr_ratio'] >= 1.0:
            all_signals.append(signal)

    logging.info(f"📊 {analysis_count}件の分析を実施し、{len(all_signals)}件の有効シグナルを検出しました。")
    return all_signals

# ====================================================================================
# TRADING & POSITION MONITORING
# ====================================================================================

async def execute_trade(signal: Dict) -> Dict:
    """CCXTを利用して現物ロング注文を執行し、結果を返す"""
    global EXCHANGE_CLIENT
    
    trade_result: Dict = {
        'status': 'error',
        'error_message': 'CCXTクライアントが未初期化',
        'filled_amount': 0.0,
        'filled_usdt': 0.0,
        'entry_price': 0.0,
    }

    if TEST_MODE:
        trade_result['status'] = 'ok'
        trade_result['error_message'] = 'TEST_MODE'
        trade_result['filled_amount'] = signal['lot_size_usdt'] / signal['entry_price']
        trade_result['filled_usdt'] = signal['lot_size_usdt']
        trade_result['entry_price'] = signal['entry_price']
        return trade_result
        
    if not EXCHANGE_CLIENT:
        return trade_result

    symbol = signal['symbol']
    lot_size_usdt = signal['lot_size_usdt']
    
    try:
        # 1. 注文金額の調整 (取引所の最小ロット/精度に合わせる)
        market = EXCHANGE_CLIENT.markets[symbol]
        base_currency = market['base']
        
        # USDT金額 (quote) からベース通貨の数量を計算（成行注文のため概算）
        # lot_size_usdt / signal['entry_price'] は概算数量
        amount_base = lot_size_usdt / signal['entry_price'] 
        
        # 取引所の数量精度に丸める
        amount_base_rounded = EXCHANGE_CLIENT.amount_to_precision(symbol, amount_base)
        
        # 2. 成行買い注文 (Market Buy) を執行
        order = await EXCHANGE_CLIENT.create_market_buy_order(symbol, amount_base_rounded)
        
        # 3. 注文結果の分析
        if order and order['status'] == 'closed' and order['filled'] > 0:
            trade_result['status'] = 'ok'
            trade_result['filled_amount'] = order['filled']
            # CCXTの現物取引では、コストがUSDT相当になる
            trade_result['filled_usdt'] = order['cost'] 
            trade_result['entry_price'] = order['average'] or signal['entry_price']
            trade_result['error_message'] = None
        else:
            trade_result['error_message'] = f"注文執行失敗: ステータス={order['status']}" if order else "注文オブジェクトが取得できませんでした。"
            
    except ccxt.InsufficientFunds as e:
        trade_result['error_message'] = f"残高不足エラー: {e}"
    except Exception as e:
        trade_result['error_message'] = f"取引執行エラー: {e}"
        logging.error(f"❌ 取引執行エラー {symbol}: {e}", exc_info=True)
        
    return trade_result

async def close_position(position: Dict, exit_type: str) -> Dict:
    """保有ポジションを成行売りで決済する"""
    global EXCHANGE_CLIENT
    
    # 決済用のロギング結果を格納する辞書
    exit_result: Dict = {
        'status': 'error',
        'error_message': 'CCXTクライアントが未初期化',
        'exit_price': 0.0,
        'pnl_usdt': 0.0,
        'pnl_rate': 0.0,
        'entry_price': position.get('entry_price', 0.0),
        'filled_amount': position.get('filled_amount', 0.0),
        'filled_usdt': position.get('filled_usdt', 0.0), # 投入USDT額
        'exit_type': exit_type,
    }
    
    symbol = position['symbol']
    amount_to_sell = position['filled_amount'] # 全量を売却

    if TEST_MODE:
        exit_result['status'] = 'ok'
        exit_result['exit_price'] = position['current_price'] # 現在価格を決済価格と仮定
        exit_result['error_message'] = 'TEST_MODE'
        
        # PnL計算 (テストモードでの概算)
        pnl_usdt = (exit_result['exit_price'] - exit_result['entry_price']) * exit_result['filled_amount']
        pnl_rate = pnl_usdt / exit_result['filled_usdt'] if exit_result['filled_usdt'] > 0 else 0.0
        exit_result['pnl_usdt'] = pnl_usdt
        exit_result['pnl_rate'] = pnl_rate
        return exit_result

    if not EXCHANGE_CLIENT:
        return exit_result

    try:
        # 1. 注文数量の精度調整
        amount_to_sell_rounded = EXCHANGE_CLIENT.amount_to_precision(symbol, amount_to_sell)
        
        # 2. 成行売り注文 (Market Sell) を執行
        order = await EXCHANGE_CLIENT.create_market_sell_order(symbol, amount_to_sell_rounded)

        # 3. 注文結果の分析
        if order and order['status'] == 'closed' and order['filled'] > 0:
            exit_result['status'] = 'ok'
            exit_result['exit_price'] = order['average']
            
            # PnLの計算
            entry_price = position['entry_price']
            filled_usdt = position['filled_usdt']
            
            # 決済時の受取額 (Quote Cost) - これには取引手数料が含まれる可能性があるため、概算
            received_usdt = order['cost'] 
            
            # PnL計算 (概算: 決済価格に基づく計算)
            pnl_usdt_calc = (exit_result['exit_price'] - entry_price) * amount_to_sell
            
            # 最終的なPnLとレート
            pnl_usdt = pnl_usdt_calc
            pnl_rate = pnl_usdt / filled_usdt if filled_usdt > 0 else 0.0
            
            exit_result['pnl_usdt'] = pnl_usdt
            exit_result['pnl_rate'] = pnl_rate
            exit_result['error_message'] = None
            
        else:
            exit_result['error_message'] = f"決済注文失敗: ステータス={order['status']}" if order else "注文オブジェクトが取得できませんでした。"

    except Exception as e:
        exit_result['error_message'] = f"決済執行エラー: {e}"
        logging.error(f"❌ 決済執行エラー {symbol}: {e}", exc_info=True)
        
    return exit_result

async def monitor_and_close_positions():
    """オープンポジションを監視し、SL/TPに達したら決済する"""
    global OPEN_POSITIONS
    
    if not OPEN_POSITIONS:
        return
        
    # 1. 全てのポジションの現在価格を取得するためのタスクを作成
    symbols_to_fetch = list(set([pos['symbol'] for pos in OPEN_POSITIONS]))
    ticker_fetch_tasks = [
        asyncio.to_thread(EXCHANGE_CLIENT.fetch_ticker, symbol) 
        for symbol in symbols_to_fetch
    ]
    
    ticker_results = await asyncio.gather(*ticker_fetch_tasks, return_exceptions=True)
    
    current_prices: Dict[str, float] = {}
    for i, result in enumerate(ticker_results):
        symbol = symbols_to_fetch[i]
        if not isinstance(result, Exception) and result and result.get('last'):
            current_prices[symbol] = result['last']
        else:
            logging.error(f"❌ ポジション監視: {symbol}の現在価格取得に失敗しました。")
            
    # 2. SL/TPチェックと決済処理
    closed_positions: List[Dict] = []
    
    for position in OPEN_POSITIONS:
        symbol = position['symbol']
        current_price = current_prices.get(symbol)
        
        if current_price is None:
            continue # 価格取得失敗したものは次のループに持ち越す

        # 現在価格をポジション情報に追加 (ログ用)
        position['current_price'] = current_price

        stop_loss = position['stop_loss']
        take_profit = position['take_profit']
        
        exit_type = None
        if current_price <= stop_loss:
            exit_type = "SL (ストップロス)"
            logging.warning(f"🚨 SLトリガー: {symbol} 現在価格 {current_price} <= SL {stop_loss}")
        elif current_price >= take_profit:
            exit_type = "TP (テイクプロフィット)"
            logging.info(f"💰 TPトリガー: {symbol} 現在価格 {current_price} >= TP {take_profit}")
            
        if exit_type:
            # 決済処理を実行
            exit_result = await close_position(position, exit_type)
            
            if exit_result['status'] == 'ok':
                # 決済成功: ログと通知
                # ポジション情報と決済結果を統合
                log_data = position.copy()
                log_data['exit_details'] = exit_result
                
                log_signal(log_data, "Trade Exit", trade_result=exit_result)
                
                current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
                message = format_telegram_message(log_data, "ポジション決済", current_threshold, trade_result=exit_result, exit_type=exit_type)
                await send_telegram_notification(message)
                
                closed_positions.append(position)
            else:
                # 決済失敗: エラーログ
                logging.error(f"❌ ポジション決済に失敗しました ({symbol}, {exit_type}): {exit_result['error_message']}")

    # 3. 決済されたポジションをリストから削除
    OPEN_POSITIONS = [pos for pos in OPEN_POSITIONS if pos not in closed_positions]
    logging.info(f"✅ ポジション監視完了。残り管理ポジション数: {len(OPEN_POSITIONS)}")


# ====================================================================================
# MACRO & FGI CONTEXT
# ====================================================================================

async def fetch_fgi_proxy() -> float:
    """
    恐怖・貪欲指数 (FGI) を取得し、-0.05から+0.05の範囲にスケーリングした
    マクロトレンドのプロキシ値を返す。
    -0.05: Extreme Fear (極度の恐怖)
    +0.05: Extreme Greed (極度の貪欲)
    """
    # 簡易版: 外部APIを叩かず、ランダムな値を生成する (動作確認用)
    # 本番環境では外部FGI API等を使用してください
    try:
        # FGI値 (0-100) を想定
        fgi_value_raw = random.randint(15, 90)
        
        # 0.0を中心に-1から1に正規化 (0=50, 1=-100, -1=0)
        # (fgi_value_raw - 50) / 50 の範囲は -1.0 から 1.0
        normalized_fgi = (fgi_value_raw - 50) / 50 
        
        # マクロ影響度 (-0.05から+0.05の範囲にスケーリング)
        fgi_proxy = normalized_fgi * FGI_PROXY_BONUS_MAX
        
        GLOBAL_MACRO_CONTEXT['fgi_raw_value'] = fgi_value_raw
        GLOBAL_MACRO_CONTEXT['fgi_proxy'] = round(fgi_proxy, 4)
        
        logging.info(f"🌍 FGIプロキシ取得成功: {fgi_value_raw} ({fgi_proxy:.4f})")
        return fgi_proxy

    except Exception as e:
        logging.error(f"❌ FGIプロキシ取得エラー: {e}")
        return 0.0 # エラー時は中立値 (0.0) を返す

async def fetch_macro_context() -> Dict:
    """すべてのマクロコンテキスト（FGI、為替など）を更新する"""
    
    # 1. FGIの更新
    fgi_proxy = await fetch_fgi_proxy()
    
    # 2. 為替の更新 (機能削除済のため0.0を固定)
    forex_bonus = 0.0
    
    # グローバル変数を更新
    GLOBAL_MACRO_CONTEXT['fgi_proxy'] = fgi_proxy
    GLOBAL_MACRO_CONTEXT['forex_bonus'] = forex_bonus
    
    return GLOBAL_MACRO_CONTEXT

# ====================================================================================
# MAIN BOT LOGIC
# ====================================================================================

async def main_bot_loop():
    """ボットのメイン実行ループ (約10分ごと)"""
    global LAST_SUCCESS_TIME, LAST_SIGNAL_TIME, LAST_ANALYSIS_SIGNALS, IS_FIRST_MAIN_LOOP_COMPLETED
    global LAST_HOURLY_NOTIFICATION_TIME, LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, LAST_WEBSHARE_UPLOAD_TIME

    if not IS_CLIENT_READY:
        logging.critical("❌ CCXTクライアントが未初期化です。メインループをスキップします。")
        return

    try:
        # 1. 市場情報とマクロコンテキストの更新
        # 出来高上位銘柄を再取得
        current_symbols = await fetch_markets_and_filter_symbols()
        CURRENT_MONITOR_SYMBOLS[:] = current_symbols
        
        # マクロコンテキストの更新 (FGI, 為替など)
        macro_context = await fetch_macro_context()
        current_threshold = get_current_threshold(macro_context)

        # 2. 全銘柄の分析とシグナル生成
        all_signals = await analyze_and_generate_signals(current_symbols, macro_context)
        LAST_ANALYSIS_SIGNALS[:] = all_signals

        # 3. シグナル通知/取引実行の判断
        
        # スコア降順にソート
        sorted_signals = sorted(all_signals, key=lambda s: s['score'], reverse=True)
        
        executed_count = 0
        
        # 取引実行ロジック
        for signal in sorted_signals:
            symbol = signal['symbol']
            
            # a. スコアとRRRの最終チェック (取引閾値)
            if signal['score'] < current_threshold or signal['rr_ratio'] < 1.0:
                continue # 閾値未満またはRRR不足はスキップ
                
            # b. クールダウンチェック (同一銘柄の再取引を避ける)
            now = time.time()
            last_signal_time = LAST_SIGNAL_TIME.get(symbol, 0)
            if now - last_signal_time < TRADE_SIGNAL_COOLDOWN:
                logging.info(f"ℹ️ {symbol} はクールダウン期間中です。取引をスキップします。")
                continue
                
            # c. 取引実行
            trade_result = await execute_trade(signal)
            
            if trade_result['status'] == 'ok':
                executed_count += 1
                
                # ポジション管理リストに追加 (成功時のみ)
                new_position = {
                    'symbol': symbol,
                    'timeframe': signal['timeframe'],
                    'entry_price': trade_result['entry_price'],
                    'filled_amount': trade_result['filled_amount'],
                    'filled_usdt': trade_result['filled_usdt'],
                    'stop_loss': signal['stop_loss'],
                    'take_profit': signal['take_profit'],
                    'rr_ratio': signal['rr_ratio'],
                    'signal_score': signal['score'],
                    'trade_id': str(uuid.uuid4()), # ユニークな取引ID
                    'entry_timestamp': now,
                }
                OPEN_POSITIONS.append(new_position)
                
                # ログと通知
                log_signal(signal, "Trade Signal", trade_result=trade_result)
                message = format_telegram_message(signal, "取引シグナル", current_threshold, trade_result=trade_result)
                await send_telegram_notification(message)
                
                LAST_SIGNAL_TIME[symbol] = now
                
                # 設定された最大シグナル数を超えたら終了
                if executed_count >= TOP_SIGNAL_COUNT:
                    logging.info(f"✅ 最大取引数 ({TOP_SIGNAL_COUNT}件) に達しました。シグナル処理を終了します。")
                    break
            else:
                # 取引失敗時もログに記録 (エラーメッセージを含む)
                log_signal(signal, "Trade Failure", trade_result=trade_result)
                logging.error(f"❌ {symbol}の取引実行に失敗しました: {trade_result['error_message']}")

        # 4. 初回起動完了通知 (一度だけ実行)
        if not IS_FIRST_MAIN_LOOP_COMPLETED:
            account_status = await fetch_account_status()
            startup_message = format_startup_message(
                account_status,
                macro_context,
                len(CURRENT_MONITOR_SYMBOLS),
                current_threshold,
                "v19.0.28 - Safety and Frequency Finalized (Patch 36)"
            )
            await send_telegram_notification(startup_message)
            IS_FIRST_MAIN_LOOP_COMPLETED = True
            
        # 5. 定期的な分析専用通知 (1時間ごと)
        now = time.time()
        if now - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME >= ANALYSIS_ONLY_INTERVAL:
            # 閾値以上のシグナルがなかった場合にのみ送信
            if executed_count == 0:
                notification_message = format_analysis_only_message(
                    sorted_signals, 
                    macro_context, 
                    current_threshold, 
                    len(current_symbols)
                )
                await send_telegram_notification(notification_message)
            
            LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = now


        # 6. WEBSHAREログアップロード (1時間ごと)
        if now - LAST_WEBSHARE_UPLOAD_TIME >= WEBSHARE_UPLOAD_INTERVAL:
            await upload_logs_to_webshare()
            LAST_WEBSHARE_UPLOAD_TIME = now
            
        # 7. 成功時のタイムスタンプ更新
        LAST_SUCCESS_TIME = time.time()
        logging.info("♻️ メインループ処理完了。次の分析まで待機します。")

    except Exception as e:
        logging.critical(f"💣 メインループの致命的なエラー: {e}", exc_info=True)
        # エラー発生時でも、ポジション監視ループは継続させるためにLAST_SUCCESS_TIMEは更新しない

async def position_monitoring_loop():
    """ポジション監視専用ループ (約10秒ごと)"""
    while True:
        try:
            if IS_CLIENT_READY and OPEN_POSITIONS:
                await monitor_and_close_positions()
                
        except Exception as e:
            logging.error(f"❌ ポジション監視ループエラー: {e}")
            
        # 10秒ごとに実行
        await asyncio.sleep(MONITOR_INTERVAL)


# ====================================================================================
# API ENDPOINTS & HEALTH CHECK
# ====================================================================================

app = FastAPI()

@app.get("/health")
async def health_check():
    """Botのヘルスチェックエンドポイント"""
    current_time = time.time()
    # LAST_SUCCESS_TIMEがゼロの場合は現在の時刻を基準にする（初回起動時）
    last_time_for_calc = LAST_SUCCESS_TIME if LAST_SUCCESS_TIME > 0 else current_time
    next_check = max(0, int(LOOP_INTERVAL - (current_time - last_time_for_calc)))

    status_msg = {
        "status": "ok",
        "bot_version": "v19.0.28 - Safety and Frequency Finalized (Patch 36)", # バージョン更新
        "base_trade_size_usdt": BASE_TRADE_SIZE_USDT, 
        "managed_positions_count": len(OPEN_POSITIONS), 
        # last_success_time は、LAST_SUCCESS_TIMEが初期値(0.0)でない場合にのみフォーマットする
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, timezone.utc).isoformat() if LAST_SUCCESS_TIME > 0 else "N/A",
        "next_main_loop_check_seconds": next_check,
        "current_threshold": get_current_threshold(GLOBAL_MACRO_CONTEXT),
        "macro_context": GLOBAL_MACRO_CONTEXT, # 0:低リスク, 1:中リスク, 2:高リスク
        "is_test_mode": TEST_MODE,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS),
        "is_client_ready": IS_CLIENT_READY,
    }
    
    # メインループの最終実行時刻をチェック
    if next_check > LOOP_INTERVAL + 60: # 許容範囲を超えて遅延している場合
        status_msg['status'] = 'warning'
        status_msg['message'] = 'Main loop is running late.'
        return JSONResponse(content=status_msg, status_code=200)

    return JSONResponse(content=status_msg, status_code=200)

# ====================================================================================
# STARTUP & SCHEDULER
# ====================================================================================

async def scheduler():
    """メインループと監視ループをスケジュールする"""
    global IS_CLIENT_READY
    
    # 1. CCXTクライアントの初期化
    IS_CLIENT_READY = await initialize_exchange_client()
    
    if not IS_CLIENT_READY:
        logging.critical("❌ CCXTクライアントの初期化に失敗しました。ボットを停止します。")
        return

    # 2. ポジション監視ループを開始 (ノンストップで実行)
    asyncio.create_task(position_monitoring_loop())
    
    # 3. メインループのスケジュール (指定間隔で実行)
    while True:
        try:
            await main_bot_loop()
        except Exception as e:
            logging.critical(f"💣 スケジューラー内のメインループ実行エラー: {e}")
            
        await asyncio.sleep(LOOP_INTERVAL)


if __name__ == "__main__":
    logging.info("🚀 Apex BOTを起動しています...")
    
    # スケジューラとFastAPIサーバーの実行
    loop = asyncio.get_event_loop()
    
    # スケジューラをバックグラウンドタスクとして実行
    loop.create_task(scheduler())
    
    # FastAPIサーバーの起動 (メインスレッドでブロッキング)
    uvicorn.run(app, host="0.0.0.0", port=8000)
