#!/usr/bin/env node
/*
 * SDK 直读每日睡眠原始 DTO（含 sleepLevels 阶段时间线）。
 *
 * 背景：插件的 get_garmin_sleep 会把 dailySleepDTO 压成 7 个字段，丢失阶段时间线。
 * 本脚本通过插件自带的 client 直接调用底层 SDK 的 getSleep(date)，把原始 JSON 打到 stdout。
 *
 * 用法（由 ingest/plugin_full.py 自动调用）：
 *   node ingest/_sdk_sleep_probe.js <plugin lib 目录> 2026-10-26 2026-10-27
 * 环境变量：GARMIN_SESSION_TOKEN_FILE / GARMIN_USERNAME / GARMIN_REGION / GARMIN_ACCOUNT
 *          / GARMIN_FIT_DOWNLOAD_DIR（与 MCP 共用）
 *
 * 输出：最后一行 JSON {"sleep": {"2026-10-26": {...dailySleepDTO...}}}
 *      失败：{"error": "..."}，退出码非 0。
 */
'use strict';

const path = require('node:path');
const fs = require('node:fs');

const libDir = process.argv[2] || process.env.PLUGIN_LIB;
const dates = process.argv.slice(3);

function die(msg, code = 1) {
  process.stdout.write(JSON.stringify({ error: String(msg).slice(0, 500) }) + '\n');
  process.exit(code);
}

if (!libDir || !fs.existsSync(path.join(libDir, 'client.js'))) {
  die('plugin lib dir not found: ' + libDir, 2);
}
if (!dates.length) die('no dates', 2);
if (!process.env.GARMIN_SESSION_TOKEN_FILE) die('GARMIN_SESSION_TOKEN_FILE not set', 2);

// session 必须是 DI v2（当前插件只在这种模式下能直接驱动底层 SDK）
try {
  const session = JSON.parse(fs.readFileSync(process.env.GARMIN_SESSION_TOKEN_FILE, 'utf8'));
  if (session.kind !== 'di-oauth' || session.schemaVersion !== 2) {
    die('session is not DI v2 (' + session.kind + '), SDK direct read unavailable', 3);
  }
} catch (e) {
  die('cannot read session: ' + e.message, 2);
}

async function main() {
  let GarminClient;
  try {
    ({ GarminClient } = require(path.join(libDir, 'client.js')));
    if (typeof GarminClient !== 'function') throw new Error('GarminClient not exported');
  } catch (e) {
    die('cannot load plugin client: ' + e.message, 4);
  }

  const cfg = {
    username: process.env.GARMIN_USERNAME || '',
    region: process.env.GARMIN_REGION || 'cn',
    account: process.env.GARMIN_ACCOUNT || 'personal-cn',
    sessionTokenFile: process.env.GARMIN_SESSION_TOKEN_FILE,
    fitDownloadDir: process.env.GARMIN_FIT_DOWNLOAD_DIR || '',
    activityDetail: 'full',
    requestTimeoutMs: 30000,
    // 插件 client 内部会写日志；logLevel 置最高只放行 error，并给它一个 ctx.logger
    logLevel: 'error',
    cacheTtl: 0,
  };
  const quiet = (...a) => process.stderr.write(a.join(' ') + '\n');
  const ctx = { logger: { debug: quiet, info: quiet, warn: quiet, error: quiet } };

  const client = new GarminClient(ctx, cfg);
  await client.connect();
  // 注意：SDK 的方法是 getSleepData(date)，且必须传 Date 对象（字符串会被 toDateString 打爆）
  const fn = typeof client.gc.getSleepData === 'function'
    ? client.gc.getSleepData.bind(client.gc)
    : null;
  if (!fn) die('SDK has no getSleepData method', 5);
  const sleep = {};
  const errors = {};
  for (const d of dates) {
    try {
      const [y, m, dd] = d.split('-').map(Number);
      // 取当天中午，避免时区把日期挪到前一天
      const raw = await fn(new Date(y, m - 1, dd, 12, 0, 0));
      if (raw) sleep[d] = raw;
    } catch (e) {
      errors[d] = String(e && e.message ? e.message : e).slice(0, 200);
    }
  }
  process.stdout.write(JSON.stringify({ sleep, errors }) + '\n');
  // 不主动关闭连接：只读查询结束后由进程退出
  process.exit(0);
}

main().catch((e) => die(e && e.stack ? e.stack : e, 1));
