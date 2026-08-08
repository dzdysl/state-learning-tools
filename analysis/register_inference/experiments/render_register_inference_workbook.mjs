#!/usr/bin/env node
/** Render a register-inference audit workbook from a normalized JSON payload. */

import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";

function argumentValue(name) {
  const index = process.argv.indexOf(name);
  if (index < 0 || index + 1 >= process.argv.length) {
    return null;
  }
  return process.argv[index + 1];
}

function excelColumn(index) {
  let value = index + 1;
  let name = "";
  while (value > 0) {
    const remainder = (value - 1) % 26;
    name = String.fromCharCode(65 + remainder) + name;
    value = Math.floor((value - 1) / 26);
  }
  return name;
}

async function sha256File(filePath) {
  const bytes = await fs.readFile(filePath);
  return crypto.createHash("sha256").update(bytes).digest("hex");
}

function gradeFill(value) {
  if (value === "relatively_stable_candidate" || value === "相对稳定") {
    return "#D9EAD3";
  }
  if (value === "hypothetical_candidate" || value === "假设性") {
    return "#FFF2CC";
  }
  return null;
}

async function main() {
  const payloadPath = argumentValue("--payload");
  const outputPath = argumentValue("--output");
  const nodeModules = argumentValue("--node-modules") || process.env.REGISTER_INFERENCE_NODE_MODULES;
  const previewDir = argumentValue("--preview-dir");
  if (!payloadPath || !outputPath || !nodeModules) {
    throw new Error("usage: --payload <json> --output <xlsx> --node-modules <directory> [--preview-dir <directory>]");
  }

  const require = createRequire(path.join(path.resolve(nodeModules), "package.json"));
  const { SpreadsheetFile, Workbook } = require("@oai/artifact-tool");
  const payload = JSON.parse(await fs.readFile(payloadPath, "utf8"));
  const workbook = Workbook.create();
  const rowCounts = {};
  const previewRanges = {};

  for (let index = 0; index < payload.sheets.length; index += 1) {
    const spec = payload.sheets[index];
    const sheet = workbook.worksheets.add(spec.name);
    const width = spec.headers.length;
    const height = spec.rows.length + 1;
    const lastColumn = excelColumn(width - 1);
    const tableRange = `A1:${lastColumn}${height}`;
    sheet.getRange(tableRange).values = [spec.headers, ...spec.rows];
    sheet.showGridLines = false;
    sheet.getRange(tableRange).format.wrapText = true;
    sheet.getRange(tableRange).format.horizontalAlignment = "center";
    sheet.getRange(tableRange).format.verticalAlignment = "center";
    sheet.getRange(`A1:${lastColumn}1`).format = {
      fill: "#0F4C5C",
      font: { bold: true, color: "#FFFFFF" },
      wrapText: true,
      borders: { preset: "outside", style: "medium", color: "#0F4C5C" },
    };
    sheet.getRange(`A2:${lastColumn}${height}`).format.borders = {
      preset: "inside", style: "thin", color: "#D9E2F3",
    };
    spec.widths.forEach((columnWidth, columnIndex) => {
      sheet.getRange(`${excelColumn(columnIndex)}1:${excelColumn(columnIndex)}${height}`).format.columnWidth = columnWidth;
    });
    sheet.getRange(`A1:${lastColumn}${height}`).format.autofitRows();
    sheet.tables.add(tableRange, true, `RegisterInference${index + 1}`);
    if (Number.isInteger(spec.candidateTypeColumn)) {
      for (let rowIndex = 0; rowIndex < spec.rows.length; rowIndex += 1) {
        const fill = gradeFill(spec.rows[rowIndex][spec.candidateTypeColumn]);
        if (fill) {
          sheet.getRange(`${excelColumn(spec.candidateTypeColumn)}${rowIndex + 2}`).format.fill = fill;
        }
      }
    }
    sheet.freezePanes.freezeRows(1);
    rowCounts[spec.name] = spec.rows.length;
    previewRanges[spec.name] = `A1:${lastColumn}${Math.min(height, 31)}`;
  }

  if (previewDir) {
    await fs.mkdir(previewDir, { recursive: true });
    for (const spec of payload.sheets) {
      const preview = await workbook.render({
        sheetName: spec.name,
        range: previewRanges[spec.name],
        autoCrop: "all",
        scale: 1,
        format: "png",
      });
      await fs.writeFile(path.join(previewDir, `${spec.name}.png`), new Uint8Array(await preview.arrayBuffer()));
    }
  }

  await fs.mkdir(path.dirname(outputPath), { recursive: true });
  const xlsx = await SpreadsheetFile.exportXlsx(workbook);
  await xlsx.save(outputPath);
  process.stdout.write(`${JSON.stringify({ sheet_rows: rowCounts, sha256: await sha256File(outputPath) })}\n`);
}

main().catch((error) => {
  process.stderr.write(`workbook-renderer error: ${error.stack || error.message}\n`);
  process.exitCode = 2;
});
