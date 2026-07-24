/**
 * EvoPi Workshop Extension
 *
 * Python agent runtime 开发辅助工具集：
 * 1. 保护敏感路径（.env 等），防止意外覆写
 * 2. /evo:check   — 完整质量门禁 (ruff → mypy → pytest → git diff --check)
 * 3. /evo:lint    — 仅 ruff 快速检查
 * 4. Python 文件修改后自动提示运行 /evo:check
 */

import { exec } from "node:child_process";
import { basename, extname } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// ---------------------------------------------------------------------------
// 配置
// ---------------------------------------------------------------------------

/** 禁止覆写的路径片段（大小写不敏感匹配） */
const PROTECTED_PATTERNS = [
  ".env", // 包含密钥，绝不可覆写
  ".env.local",
  ".env.production",
  ".git/",
  "session.jsonl",
  "checkpoints/",
];

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

function isProtected(filePath: string): boolean {
  const lower = filePath.toLowerCase();
  return PROTECTED_PATTERNS.some((p) => lower.includes(p.toLowerCase()));
}

function isPythonFile(filePath: string): boolean {
  return extname(filePath).toLowerCase() === ".py";
}

/** 异步执行命令，返回 { ok, output } */
function runCmd(cmd: string, args: string[]): Promise<{ ok: boolean; output: string }> {
  return new Promise((resolve) => {
    const fullCmd = [cmd, ...args].join(" ");
    exec(fullCmd, {
      encoding: "utf-8",
      timeout: 90_000,
      windowsHide: true,
    }, (error, stdout, stderr) => {
      if (error) {
        const msg = [stderr.trim(), stdout.trim()].filter(Boolean).join("\n") || String(error);
        resolve({ ok: false, output: msg });
      } else {
        resolve({ ok: true, output: stdout.trim() || "(no output)" });
      }
    });
  });
}

// ---------------------------------------------------------------------------
// 扩展入口
// ---------------------------------------------------------------------------

export default function (pi: ExtensionAPI) {
  // =========================================================================
  // 1. 路径保护：阻止覆写敏感文件
  // =========================================================================
  pi.on("tool_call", async (event, ctx) => {
    if (event.toolName !== "write" && event.toolName !== "edit") return;

    const filePath: string | undefined =
      (event.input as any)?.path ?? (event.input as any)?.file_path;

    if (filePath && isProtected(filePath)) {
      if (ctx.hasUI) {
        ctx.ui.notify(
          `🛡 已阻止覆写受保护路径：${basename(filePath)}`,
          "warning",
        );
      }
      return {
        block: true,
        reason: `路径 "${filePath}" 受 EvoPi Workshop 保护。密钥文件不可通过工具覆写。`,
      };
    }
  });

  // =========================================================================
  // 2. /evo:lint — 快速 ruff 检查
  // =========================================================================
  pi.registerCommand("evo:lint", {
    description: "仅运行 ruff check（快速 lint）",
    async handler(_args, ctx) {
      ctx.ui.notify("🔍 ruff check 中...", "info");
      const result = await runCmd("python", ["-m", "ruff", "check", "."]);

      if (result.ok) {
        ctx.ui.notify("✅ ruff 通过", "success");
      } else {
        ctx.ui.notify("❌ ruff 发现问题", "error");
        console.error(result.output);
      }
      return result.output || (result.ok ? "ruff: 通过" : "ruff: 失败");
    },
  });

  // =========================================================================
  // 3. /evo:check — 完整质量门禁
  // =========================================================================
  pi.registerCommand("evo:check", {
    description: "完整质量门禁：ruff → mypy → pytest → git diff --check",
    async handler(_args, ctx) {
      const steps: { label: string; run: () => Promise<{ ok: boolean; output: string }> }[] = [
        { label: "ruff check", run: () => runCmd("python", ["-m", "ruff", "check", "."]) },
        { label: "mypy", run: () => runCmd("python", ["-m", "mypy"]) },
        { label: "pytest", run: () => runCmd("python", ["-m", "pytest", "-q"]) },
        { label: "git diff --check", run: () => runCmd("git", ["diff", "--check"]) },
      ];

      const results: string[] = [];
      let allPassed = true;

      for (const step of steps) {
        ctx.ui.setStatus("evo:check", `⏳ ${step.label}...`);
        ctx.ui.notify(`⏳ 正在运行 ${step.label}...`, "info");
        const r = await step.run();
        results.push(`--- ${step.label} ---\n${r.output}`);
        if (!r.ok) {
          allPassed = false;
          ctx.ui.notify(`❌ ${step.label} 失败`, "error");
          break; // 失败即停
        }
        ctx.ui.notify(`✅ ${step.label} 通过`, "success");
      }

      ctx.ui.setStatus("evo:check", allPassed ? "✅ all passed" : "❌ failed");

      const summary = allPassed
        ? "✅ 质量门禁全部通过！"
        : "❌ 质量门禁未通过，请查看上方错误。";
      ctx.ui.notify(summary, allPassed ? "success" : "error");

      return results.join("\n\n") + "\n\n" + summary;
    },
  });

  // =========================================================================
  // 4. Python 文件修改后自动提示
  // =========================================================================
  let pythonFilesTouched = 0;

  pi.on("tool_execution_end", async (event, ctx) => {
    if (event.toolName !== "write" && event.toolName !== "edit") return;

    const filePath: string | undefined =
      (event.input as any)?.path ?? (event.input as any)?.file_path;

    if (filePath && isPythonFile(filePath)) {
      pythonFilesTouched++;
    }
  });

  pi.on("turn_end", async (_event, ctx) => {
    if (pythonFilesTouched > 0 && ctx.hasUI) {
      ctx.ui.notify(
        `📝 已修改 ${pythonFilesTouched} 个 Python 文件 — 建议运行 /evo:check`,
        "info",
      );
      pythonFilesTouched = 0;
    }
  });

  // =========================================================================
  // 启动提示
  // =========================================================================
  pi.on("session_start", async (_event, ctx) => {
    if (ctx.hasUI) {
      ctx.ui.notify(
        "🛠 EvoPi Workshop 已加载 | /evo:check 完整门禁 | /evo:lint 快速 lint",
        "info",
      );
    }
  });
}
