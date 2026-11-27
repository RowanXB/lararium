// A category is whatever text a transaction carries (today the source's own, such as Monzo's
// "eating_out"), never one of a fixed list: categories the owner defines later show up here
// without a change. This is the one place that turns one into words.
export function categoryLabel(category: string | null): string {
  if (category === null || category.trim() === "") return "Uncategorised";
  const words = category.replace(/[_-]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}
