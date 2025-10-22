# ====================================================================================
# Apex BOT v19.0.28 - Safety and Frequency Finalized (Patch 36)
#
# 修正ポイント:
# 1. 【エラー修正】FTPアップロード関数 (_sync_ftp_upload) に最大3回のリトライロジックを追加し、タイムアウトエラーに対応。
# 2. 【堅牢化】OHLCV取得関数 (fetch_ohlcv_safe) のエラーハンドリングを強化し、CCXT APIエラーやレート制限に対応。
# 3. 【安全確認】動的取引閾値 (0.67, 0.63, 0.58) を最終確定。
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
import ftplib 
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


# 💡 WEBSHARE設定 (FTP/WebDAVなど、外部ログストレージを想定)
WEBSHARE_HOST = os.getenv("WEBSHARE_HOST")
WEBSHARE_PORT = int(os.getenv("WEBSHARE_PORT", "21")) # デフォルトはFTPポート
WEBSHARE_USER = os.getenv("WEBSHARE_USER")
WEBSHARE_PASS = os.getenv("WEBSHARE_PASS")

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


def _sync_ftp_upload(local_file: str, remote_file: str):
    """
    同期的にFTPアップロードを実行するヘルパー関数。
    asyncio.to_threadで使用される。
    💡 【修正点】リトライロジックを追加し、タイムアウトエラーに対応。
    """
    MAX_RETRIES = 3 
    RETRY_DELAY = 5 
    
    if not WEBSHARE_HOST or not WEBSHARE_USER or not WEBSHARE_PASS:
        logging.error("❌ WEBSHARE設定 (HOST/USER/PASS) が不足しています。")
        return False
    if not os.path.exists(local_file):
        logging.warning(f"⚠️ ローカルファイル {local_file} が見つかりません。アップロードをスキップします。")
        return True 

    for attempt in range(MAX_RETRIES):
        ftp = None
        try:
            # FTP接続とログイン
            ftp = ftplib.FTP()
            # タイムアウトを30秒に設定
            ftp.connect(WEBSHARE_HOST, WEBSHARE_PORT, timeout=30)
            ftp.login(WEBSHARE_USER, WEBSHARE_PASS)
            
            # ファイルのアップロード (バイナリモード)
            with open(local_file, 'rb') as fp:
                ftp.storbinary(f'STOR {remote_file}', fp)
            
            ftp.quit()
            logging.info(f"✅ FTPアップロード成功 (試行{attempt+1}回目): {local_file} -> {remote_file}")
            return True
        except ftplib.all_errors as e:
            logging.error(f"❌ FTPアップロードエラー ({WEBSHARE_HOST}, 試行{attempt+1}/{MAX_RETRIES}): {e}")
            if ftp:
                try:
                    ftp.close()
                except:
                    pass
            if attempt < MAX_RETRIES - 1:
                logging.info(f"🔄 {RETRY_DELAY}秒後にFTPアップロードをリトライします...")
                time.sleep(RETRY_DELAY)
            else:
                # 最終試行で失敗
                return False 
        except Exception as e:
            logging.error(f"❌ ログアップロードの予期せぬエラー: {e}")
            if ftp:
                try:
                    ftp.close()
                except:
                    pass
            return False 
    return False 


async def upload_logs_to_webshare():
    """ローカルログファイルを外部ストレージ (WebShare/FTP) にアップロードする"""
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
    
    logging.info(f"📤 WEBSHAREログアップロード処理を開始します...")

    tasks = []
    for log_file in log_files:
        if os.path.exists(log_file):
            # リモートファイル名にはタイムスタンプとファイル名を含める
            remote_filename = f"apex_log_{upload_timestamp}_{log_file}"
            # 同期FTP処理を別スレッドで実行
            tasks.append(
                asyncio.to_thread(_sync_ftp_upload, log_file, remote_filename)
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
            # MEXCなど一部の取引所向けの設定をここに追加可能
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
    """
    OHLCVデータを安全に取得する。
    💡 【修正点】より詳細なエラーハンドリングを追加。
    """
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return None
    
    try:
        # fetch_ohlcv は通常のリトライ機構を持っているが、ここでは外部エラーのみを扱う
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        if not ohlcv or len(ohlcv) < 50: # データ不足もここで検出
             logging.warning(f"⚠️ {symbol} - {timeframe}データ不足または計算エラー。分析をスキップします。")
             return None

        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        
        # 最終的なデータフレームとして返す
        return df
        
    except ccxt.DDoSProtection as e:
        logging.error(f"❌ OHLCV取得エラー (DDoS/レート制限 - {symbol} {timeframe}): {EXCHANGE_CLIENT.id} {e}")
        # レート制限エラーの場合は少し待機
        await asyncio.sleep(EXCHANGE_CLIENT.rateLimit / 1000)
        return None
    except ccxt.ExchangeNotAvailable as e:
        logging.error(f"❌ OHLCV取得エラー (取引所API停止中 - {symbol} {timeframe}): {EXCHANGE_CLIENT.id} {e}")
        return None
    except ccxt.ExchangeError as e:
        # ログに示されたエラーの原因（例: symbol not found, invalid symbol）をキャッチ
        logging.error(f"❌ OHLCV取得エラー ({symbol} {timeframe}): {EXCHANGE_CLIENT.id} {e}")
        return None
    except Exception as e:
        logging.error(f"❌ 予期せぬOHLCV取得エラー ({symbol} {timeframe}): {e}")
        return None

async def fetch_account_status() -> Dict:
    """口座残高とポジション情報を取得する (Spot取引向け)"""
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    status = {
        'total_usdt_balance': 0.0,
        'open_positions': [],
        'error': False,
    }
    
    if not EXCHANGE_CLIENT:
        status['error'] = True
        return status
        
    try:
        # 1. 残高の取得
        balance = await EXCHANGE_CLIENT.fetch_balance()
        status['total_usdt_balance'] = balance.get('total', {}).get('USDT', 0.0)
        
        # 2. 現物ポジションの簡易チェック (CCXTはSpotポジションという概念がないため、残高をポジションと見なす)
        # 10 USDT以上の残高がある資産を現物ポジションとしてリストアップ
        for currency, info in balance.get('total', {}).items():
            if currency == 'USDT' or info <= 0:
                continue
                
            symbol = f"{currency}/USDT"
            
            # 価格を取得してUSDT価値を計算 (これはコストが高い操作なので、注意が必要)
            ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
            usdt_value = info * ticker['close']
            
            if usdt_value >= 10.0:
                 status['open_positions'].append({
                    'symbol': symbol,
                    'amount': info,
                    'usdt_value': usdt_value
                })
        
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得エラー: {e}")
        status['error'] = True
        
    return status

async def get_top_volume_symbols() -> List[str]:
    """出来高に基づいて監視対象の銘柄リストを更新する"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or SKIP_MARKET_UPDATE:
        logging.info(f"ℹ️ 市場更新をスキップします。デフォルトの {len(DEFAULT_SYMBOLS)} 銘柄を使用します。")
        return DEFAULT_SYMBOLS
        
    try:
        # 全ティッカーを取得
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        # Spot取引かつUSDTペアのみをフィルタリング
        spot_usdt_tickers = {
            symbol: ticker for symbol, ticker in tickers.items()
            if symbol.endswith('/USDT') and 'spot' in EXCHANGE_CLIENT.markets.get(symbol, {}).get('type', '')
        }
        
        # 24時間出来高 (quote volume) でソート
        sorted_tickers = sorted(
            spot_usdt_tickers.items(), 
            key=lambda item: item[1].get('quoteVolume', 0), 
            reverse=True
        )
        
        # TOP_SYMBOL_LIMITまで選択
        top_symbols = [symbol for symbol, _ in sorted_tickers[:TOP_SYMBOL_LIMIT]]
        
        # デフォルトシンボルを含め、重複を排除
        final_symbols = list(set(top_symbols + DEFAULT_SYMBOLS))

        logging.info(f"✅ 市場リストを更新しました。合計 {len(final_symbols)} 銘柄が検出されました。")
        return final_symbols
        
    except Exception as e:
        logging.error(f"❌ 市場リスト更新エラー: {e}。デフォルトのリストを使用します。")
        return DEFAULT_SYMBOLS


# ====================================================================================
# MACRO & TECHNICAL ANALYSIS (コアロジック - Placeholder)
# ====================================================================================

async def get_fgi_proxy() -> Dict:
    """外部APIからFGI（恐怖・貪欲指数）のプロキシ値を取得する (代替機能)"""
    # リアルなFGIデータ取得ロジックは省略し、ダミーデータを使用
    # 実際には外部API (Alternative.meなど) から取得する
    fgi_raw_value = random.randint(10, 90) # 10 (Extreme Fear) - 90 (Extreme Greed)
    fgi_proxy_normalized = (fgi_raw_value - 50) / 50.0 # -0.8から+0.8の範囲に正規化
    
    # Forex機能は削除されているため、常に0.0
    forex_bonus = 0.0

    return {
        'fgi_raw_value': fgi_raw_value,
        'fgi_proxy': fgi_proxy_normalized,
        'forex_bonus': forex_bonus,
        'timestamp': time.time()
    }

def calculate_technical_score(df_15m: pd.DataFrame, df_1h: pd.DataFrame, df_4h: pd.DataFrame, symbol: str) -> Optional[Dict]:
    """
    複数の時間足のデータを統合し、テクニカル分析スコアを計算する。
    リワード/リスク比率 (RRR) が1.0以上のロングシグナルのみを生成する。
    """
    
    # 💡 【コアロジックPlaceholder】
    # 実際にはここにRSI, MACD, SMA, ボリンジャーバンド、出来高分析など、
    # 複雑なテクニカル分析とスコアリングロジック（ベーススコア+ボーナス/ペナルティ）が入る。

    # データ不足チェック (分析スキップ警告の原因)
    required_data_points = {
        '15m': len(df_15m),
        '1h': len(df_1h),
        '4h': len(df_4h),
    }
    
    # 簡易データ不足チェック
    if any(count < REQUIRED_OHLCV_LIMITS['15m'] for count in required_data_points.values()):
        # ログにあるような 'RSI', 'ATR' 欠落のケースをシミュレート
        if len(df_15m) < 200: 
             logging.warning(f"⚠️ {symbol} - 15mデータ不足または計算エラー。分析をスキップします。欠落: ['RSI', 'ATR']")
             return None

    # --- ダミーのスコア/RRR計算 ---
    
    # 最終ローソク足の終値
    current_price = df_15m['close'].iloc[-1]
    
    # ダミーのSL/TP (RRR >= 1.0を強制)
    risk_percentage = random.uniform(0.005, 0.02) # リスク0.5% - 2.0%
    reward_percentage = risk_percentage * random.uniform(1.0, 3.0) # RRR 1.0 - 3.0
    
    stop_loss = current_price * (1 - risk_percentage)
    take_profit = current_price * (1 + reward_percentage)
    rr_ratio = reward_percentage / risk_percentage

    # ダミーのスコア計算 (動的閾値 0.58-0.67 を超える可能性があるように設定)
    base_score_plus_random = BASE_SCORE + random.uniform(-0.05, 0.30)
    final_score = round(min(1.0, base_score_plus_random), 4)

    # ダミーのスコア内訳 (get_score_breakdownで利用)
    tech_data = {
        'long_term_reversal_penalty_value': 0.0 if final_score > 0.65 else LONG_TERM_REVERSAL_PENALTY,
        'structural_pivot_bonus': STRUCTURAL_PIVOT_BONUS,
        'macd_penalty_value': 0.0 if final_score > 0.60 else MACD_CROSS_PENALTY,
        'obv_momentum_bonus_value': OBV_MOMENTUM_BONUS,
        'liquidity_bonus_value': LIQUIDITY_BONUS_MAX,
        'sentiment_fgi_proxy_bonus': GLOBAL_MACRO_CONTEXT.get('fgi_proxy', 0.0) * FGI_PROXY_BONUS_MAX,
        'forex_bonus': 0.0,
        'volatility_penalty_value': -0.01 if final_score > 0.85 else 0.0,
    }
    
    # --- ダミー処理終了 ---
    
    if rr_ratio < 1.0:
        return None # RRR < 1.0 はシグナルとしない

    signal = {
        'symbol': symbol,
        'timeframe': '15m', # 最も高頻度の時間足をシグナルとする
        'score': final_score,
        'entry_price': current_price,
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'rr_ratio': rr_ratio,
        'tech_data': tech_data,
        'lot_size_usdt': BASE_TRADE_SIZE_USDT * (1.0 + (final_score - BASE_SCORE) * 0.5) # スコアに応じた動的ロット
    }
    
    return signal

# ====================================================================================
# TRADING EXECUTION (Placeholder)
# ====================================================================================

async def execute_trade(signal: Dict) -> Dict:
    """CCXTを使用して現物ロング (成行買い) を実行する"""
    global EXCHANGE_CLIENT
    
    if TEST_MODE or not EXCHANGE_CLIENT:
        return {'status': 'skip', 'error_message': 'Test Mode or Client Not Ready'}
        
    symbol = signal['symbol']
    entry_usdt = signal['lot_size_usdt']
    
    # 💡 【コアロジックPlaceholder】
    # 実際にはCCXTのcreate_order関数を使用して成行注文を執行するロジックが入る
    try:
        # プレースホルダーとして、成功をシミュレート
        filled_amount = entry_usdt / signal['entry_price']
        filled_usdt = filled_amount * signal['entry_price']
        
        # 実行成功したポジションをOPEN_POSITIONSに追加
        position_id = str(uuid.uuid4())
        
        OPEN_POSITIONS.append({
            'id': position_id,
            'symbol': symbol,
            'entry_price': signal['entry_price'],
            'filled_amount': filled_amount,
            'filled_usdt': filled_usdt,
            'stop_loss': signal['stop_loss'],
            'take_profit': signal['take_profit'],
            'rr_ratio': signal['rr_ratio'],
            'timestamp': time.time(),
        })

        return {
            'status': 'ok',
            'filled_amount': filled_amount,
            'filled_usdt': filled_usdt,
            'entry_price': signal['entry_price'],
            'error_message': None
        }
        
    except ccxt.InsufficientFunds as e:
        return {'status': 'error', 'error_message': f'資金不足: {e}'}
    except ccxt.ExchangeError as e:
        return {'status': 'error', 'error_message': f'取引所APIエラー: {e}'}
    except Exception as e:
        return {'status': 'error', 'error_message': f'予期せぬ取引エラー: {e}'}

# ====================================================================================
# MAIN BOT LOOPS
# ====================================================================================

async def monitor_positions_loop():
    """オープンポジションのSL/TPを常時監視し、トリガー時に決済する (メインボットと並行稼働)"""
    global OPEN_POSITIONS, EXCHANGE_CLIENT
    
    while True:
        await asyncio.sleep(MONITOR_INTERVAL)
        
        if not OPEN_POSITIONS or not EXCHANGE_CLIENT:
            continue
            
        logging.info(f"👀 ポジション監視中... 現在 {len(OPEN_POSITIONS)} 銘柄を管理。")
        
        # リアルタイム価格の取得（まとめて行うことでレート制限を緩和）
        symbols_to_fetch = [pos['symbol'] for pos in OPEN_POSITIONS]
        if not symbols_to_fetch:
            continue

        try:
            tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols_to_fetch)
        except Exception as e:
            logging.error(f"❌ 監視中の価格取得エラー: {e}")
            continue

        positions_to_keep = []
        
        for pos in OPEN_POSITIONS:
            symbol = pos['symbol']
            ticker = tickers.get(symbol)
            
            if not ticker:
                positions_to_keep.append(pos)
                continue
                
            current_price = ticker['close']
            exit_trigger = None
            
            # 1. 損切り (SL) チェック
            if current_price <= pos['stop_loss']:
                exit_trigger = "STOP LOSS (SL)"
                
            # 2. 利益確定 (TP) チェック
            elif current_price >= pos['take_profit']:
                exit_trigger = "TAKE PROFIT (TP)"
                
            if exit_trigger:
                logging.warning(f"🔴 {symbol} - {exit_trigger} トリガー ({current_price:.6f})")
                
                # 💡 【コアロジックPlaceholder】
                # 実際にはCCXTで現物売り (成行) を実行するロジックが入る
                
                # 決済をシミュレート
                pnl_usdt = (current_price - pos['entry_price']) * pos['filled_amount']
                pnl_rate = (current_price - pos['entry_price']) / pos['entry_price']
                
                trade_result = {
                    'status': 'ok',
                    'entry_price': pos['entry_price'],
                    'exit_price': current_price,
                    'filled_amount': pos['filled_amount'],
                    'pnl_usdt': pnl_usdt,
                    'pnl_rate': pnl_rate,
                }
                
                # Telegram通知とログ
                exit_signal = {
                    'symbol': symbol,
                    'timeframe': 'N/A', # 決済に時間足は無関係
                    'score': 0.0, # 決済にスコアは無関係
                    'rr_ratio': pos['rr_ratio'],
                    # ログ関数がシグナル形式を要求するため、ダミーデータを渡す
                }
                
                message = format_telegram_message(
                    exit_signal, 
                    context="ポジション決済", 
                    current_threshold=get_current_threshold(GLOBAL_MACRO_CONTEXT), 
                    trade_result=trade_result, 
                    exit_type=exit_trigger
                )
                await send_telegram_notification(message)
                log_signal(exit_signal, "Trade Exit", trade_result=trade_result)

                # ポジションリストから削除
            else:
                positions_to_keep.append(pos)

        OPEN_POSITIONS = positions_to_keep # 監視を継続するポジションのみを残す

async def main_bot_loop():
    """メインの市場分析と取引実行ループ"""
    global CURRENT_MONITOR_SYMBOLS, LAST_SUCCESS_TIME, LAST_SIGNAL_TIME
    global GLOBAL_MACRO_CONTEXT, LAST_ANALYSIS_SIGNALS, LAST_HOURLY_NOTIFICATION_TIME
    global IS_FIRST_MAIN_LOOP_COMPLETED, LAST_WEBSHARE_UPLOAD_TIME, LAST_ANALYSIS_ONLY_NOTIFICATION_TIME
    
    while True:
        start_time = time.time()
        
        try:
            logging.info("--- メインボットループ開始 ---")
            
            # 1. マクロコンテキストの更新 (FGIなど)
            GLOBAL_MACRO_CONTEXT.update(await get_fgi_proxy())
            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)

            # 2. 監視銘柄リストの更新 (一定間隔、または初回のみ)
            if not IS_FIRST_MAIN_LOOP_COMPLETED or time.time() - LAST_SUCCESS_TIME > 60 * 60 * 4: # 4時間ごと
                CURRENT_MONITOR_SYMBOLS = await get_top_volume_symbols()
            
            # 3. 全銘柄の分析実行
            all_signals: List[Dict] = []
            
            # 銘柄ごとに並行してデータ取得と分析を行う
            tasks = []
            for symbol in CURRENT_MONITOR_SYMBOLS:
                async def analyze_symbol(s):
                    # 必要なすべての時間足のデータを取得
                    df_15m = await fetch_ohlcv_safe(s, '15m', REQUIRED_OHLCV_LIMITS['15m'])
                    df_1h = await fetch_ohlcv_safe(s, '1h', REQUIRED_OHLCV_LIMITS['1h'])
                    df_4h = await fetch_ohlcv_safe(s, '4h', REQUIRED_OHLCV_LIMITS['4h'])
                    
                    if df_15m is None or df_1h is None or df_4h is None:
                        return None # データ不足やエラーでスキップ
                    
                    # スコア計算
                    return calculate_technical_score(df_15m, df_1h, df_4h, s)

                tasks.append(analyze_symbol(symbol))
            
            # すべての分析タスクの結果を待つ
            raw_results = await asyncio.gather(*tasks)
            all_signals = [res for res in raw_results if res is not None]
            
            # スコアとRRRでソートし、分析結果をグローバル変数に格納
            LAST_ANALYSIS_SIGNALS = sorted(
                all_signals, 
                key=lambda s: s['score'], 
                reverse=True
            )
            
            # 4. 分析に基づく取引シグナルの選定と実行
            top_signals_for_trade = [
                s for s in LAST_ANALYSIS_SIGNALS
                if s['score'] >= current_threshold
                and s['rr_ratio'] >= 1.0
                and time.time() - LAST_SIGNAL_TIME.get(s['symbol'], 0) > TRADE_SIGNAL_COOLDOWN
            ][:TOP_SIGNAL_COUNT]
            
            if top_signals_for_trade:
                logging.info(f"🔥 取引閾値 ({current_threshold*100:.2f}点) を超える {len(top_signals_for_trade)} 件のシグナルを検出しました。")
                
                for signal in top_signals_for_trade:
                    # 取引実行
                    trade_result = await execute_trade(signal)
                    
                    # Telegram通知とログ
                    message = format_telegram_message(
                        signal, 
                        context="取引シグナル", 
                        current_threshold=current_threshold, 
                        trade_result=trade_result
                    )
                    await send_telegram_notification(message)
                    log_signal(signal, "Trade Signal", trade_result=trade_result)
                    
                    # クールダウン時間を更新
                    LAST_SIGNAL_TIME[signal['symbol']] = time.time()

            else:
                logging.info(f"ℹ️ 取引閾値 ({current_threshold*100:.2f}点) を超えるシグナルは見つかりませんでした。")
                
            # 5. 初回完了通知 (起動完了通知)
            if not IS_FIRST_MAIN_LOOP_COMPLETED:
                account_status = await fetch_account_status()
                startup_message = format_startup_message(
                    account_status,
                    GLOBAL_MACRO_CONTEXT,
                    len(CURRENT_MONITOR_SYMBOLS),
                    current_threshold,
                    "v19.0.28 - Safety and Frequency Finalized (Patch 36)"
                )
                await send_telegram_notification(startup_message)
                IS_FIRST_MAIN_LOOP_COMPLETED = True

            # 6. 1時間ごとの分析専用通知
            if time.time() - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME >= ANALYSIS_ONLY_INTERVAL:
                logging.info("⏳ 1時間ごとの分析通知を準備します...")
                
                # 分析専用のログファイルに記録
                if LAST_ANALYSIS_SIGNALS:
                    log_signal(LAST_ANALYSIS_SIGNALS[0], "Hourly Analysis")
                
                analysis_message = format_analysis_only_message(
                    LAST_ANALYSIS_SIGNALS,
                    GLOBAL_MACRO_CONTEXT,
                    current_threshold,
                    len(CURRENT_MONITOR_SYMBOLS)
                )
                await send_telegram_notification(analysis_message)
                LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = time.time()
                
            # 7. WebShareへのログアップロード
            if time.time() - LAST_WEBSHARE_UPLOAD_TIME >= WEBSHARE_UPLOAD_INTERVAL:
                await upload_logs_to_webshare()
                LAST_WEBSHARE_UPLOAD_TIME = time.time()

        except Exception as e:
            logging.error(f"❌ メインボットループで予期せぬ致命的なエラーが発生: {e}", exc_info=True)
            # エラー発生時は一旦待機
            await asyncio.sleep(60)
            
        finally:
            end_time = time.time()
            elapsed = end_time - start_time
            LAST_SUCCESS_TIME = end_time
            
            wait_time = max(0, LOOP_INTERVAL - elapsed)
            
            logging.info("--- メインボットループ終了 ---")
            logging.info(f"😴 メインループを {elapsed:.2f}秒 で完了しました。次回の実行まで {wait_time:.0f}秒 待機します。")
            await asyncio.sleep(wait_time)


# ====================================================================================
# FASTAPI & ENTRYPOINT
# ====================================================================================

app = FastAPI()

@app.on_event("startup")
async def startup_event():
    """FastAPIの起動時にBOTの初期化とタスクの起動を行う"""
    logging.info("--- FastAPI Startup Event: BOT初期化開始 ---")
    
    # 1. CCXTクライアントの初期化
    global IS_CLIENT_READY
    IS_CLIENT_READY = await initialize_exchange_client()
    
    if IS_CLIENT_READY:
        # 2. 監視銘柄リストの初期ロード
        global CURRENT_MONITOR_SYMBOLS
        CURRENT_MONITOR_SYMBOLS = await get_top_volume_symbols()
        
        # 3. BOTのメインタスクとポジション監視タスクを起動
        asyncio.create_task(main_bot_loop())
        asyncio.create_task(monitor_positions_loop())
        
        logging.info("✅ BOTメインタスクと監視タスクを起動しました。")
        logging.info("--- メインボットループ開始 ---")
    else:
        logging.critical("❌ BOTのメインタスクは起動されませんでした。CCXTクライアントの初期化に失敗しています。")

@app.get("/status")
async def get_status():
    """BOTの現在のステータスを返すAPIエンドポイント"""
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
        "is_client_ready": IS_CLIENT_READY
    }
    return JSONResponse(content=status_msg)

if __name__ == "__main__":
    # 環境変数からポート番号を取得し、Uvicornを起動
    port = int(os.getenv("PORT", 8080))
    # 'main_render:app' の部分は、このファイル名が main_render.py であることを想定
    uvicorn.run("main_render:app", host="0.0.0.0", port=port)
