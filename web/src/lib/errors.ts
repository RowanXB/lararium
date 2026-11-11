// Finance API errors look like {"detail": {"error": "<code>", "message": "…"}}, and
// validation errors like {"detail": [{"msg": "…"}]}. The published OpenAPI doesn't
// describe these bodies yet, so read them loosely.
export function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "object" && error !== null && "detail" in error) {
    const detail = (error as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((item: unknown) =>
          typeof item === "object" && item !== null && "msg" in item ? String(item.msg) : "",
        )
        .filter(Boolean)
        .join("; ");
    }
    if (typeof detail === "object" && detail !== null) {
      if ("message" in detail && typeof detail.message === "string") return detail.message;
      if ("error" in detail && typeof detail.error === "string") return detail.error;
    }
  }
  return "Request failed";
}
