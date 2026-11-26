// Chart colours: the same values as the theme in index.css, which a canvas can't read.
export const CHART = {
  accent: "#7aa2ff",
  income: "#3dd6a3",
  spending: "#ff9466",
  warning: "#f5c05b",
  text: "#8c96b3",
  line: "#232c4a",
  surface: "#151d38",
  foreground: "#e8ecf7",
  // Categories, in order; they repeat past the last.
  series: ["#7aa2ff", "#3dd6a3", "#ff9466", "#b48cff", "#f5c05b", "#5cc8e6", "#ff7aa8", "#8c96b3"],
} as const;

// What every chart shares: a quiet axis and a tooltip that matches the cards.
export const AXIS = {
  axisLine: { lineStyle: { color: CHART.line } },
  axisTick: { show: false },
  axisLabel: { color: CHART.text },
  splitLine: { lineStyle: { color: CHART.line, type: "dashed" as const } },
};

export const TOOLTIP = {
  backgroundColor: CHART.surface,
  borderColor: CHART.line,
  textStyle: { color: CHART.foreground },
};
