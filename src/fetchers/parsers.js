"use strict";
/** 响应体解析工具：JSON、CSV、新浪 var、东财 newfunddata。*/

function asJson(body) {
  if (body == null) return null;
  if (typeof body === "object") return body;
  try {
    return JSON.parse(body);
  } catch (_) {
    return null;
  }
}

/** 简易 CSV 解析：处理带引号字段、引号内逗号/转义双引号。返回二维数组。*/
function parseCsv(text) {
  if (!text) return [];
  const lines = String(text).split(/\r?\n/).filter((l) => l.trim().length > 0);
  return lines.map((line) => {
    const out = [];
    let cur = "";
    let inQ = false;
    for (let i = 0; i < line.length; i++) {
      const c = line[i];
      if (inQ) {
        if (c === '"') {
          if (line[i + 1] === '"') {
            cur += '"';
            i++;
          } else {
            inQ = false;
          }
        } else {
          cur += c;
        }
      } else if (c === '"') {
        inQ = true;
      } else if (c === ",") {
        out.push(cur);
        cur = "";
      } else {
        cur += c;
      }
    }
    out.push(cur);
    return out;
  });
}

/** 新浪 hq.sinajs.cn：`var hq_str_xxx="a,b,c";` → 取引号内字符串并按逗号切分。*/
function parseSina(body) {
  const m = String(body || "").match(/="([^"]*)"/);
  if (!m) return null;
  return m[1].split(",");
}

/** 东财基金新发行：`var newfunddata={datas:[...],count:"1"};` → 解析为对象。
 * 东财返回的是 JS 字面量（顶层 key 不带引号），不是严格 JSON，需先把裸 key 补上引号。
 * 值均为带引号的字符串，因此只对 `{key:` / `,key:` 形态做替换是安全的。*/
function parseNewfunddata(body) {
  const raw = String(body || "").trim();
  const idx = raw.indexOf("=");
  if (idx < 0) return null;
  let jsonStr = raw.slice(idx + 1).trim();
  if (jsonStr.endsWith(";")) jsonStr = jsonStr.slice(0, -1).trim();
  // 给顶层及嵌套对象的裸 key 加引号：`{key:` / `,key:`
  jsonStr = jsonStr.replace(/([{,])\s*([A-Za-z_][A-Za-z0-9_]*)\s*:/g, '$1"$2":');
  return asJson(jsonStr);
}

/** 把东财 datacenter 的字符串数字安全转 float；失败返回 null。*/
function toFloat(v) {
  if (v == null || v === "" || v === "-") return null;
  const n = parseFloat(String(v).replace(/,/g, ""));
  return Number.isFinite(n) ? n : null;
}

module.exports = { asJson, parseCsv, parseSina, parseNewfunddata, toFloat };
