"use server";

import { revalidatePath } from "next/cache";
import { supabaseAdmin } from "@/lib/supabase/admin";

export async function toggleTag(tradeId: string, tagId: string, on: boolean) {
  const db = supabaseAdmin();
  if (on) {
    await db.from("trade_tags").upsert({ trade_id: tradeId, tag_id: tagId }, { onConflict: "trade_id,tag_id" });
  } else {
    await db.from("trade_tags").delete().eq("trade_id", tradeId).eq("tag_id", tagId);
  }
  revalidatePath(`/journal/${tradeId}`);
}

export async function uploadScreenshot(formData: FormData) {
  const tradeId = String(formData.get("trade_id"));
  const file = formData.get("file") as File | null;
  if (!file || file.size === 0 || file.size > 10 * 1024 * 1024) return;
  const db = supabaseAdmin();
  const ext = (file.name.split(".").pop() || "png").toLowerCase().replace(/[^a-z0-9]/g, "");
  const path = `${tradeId}/${Date.now()}.${ext}`;
  const { error } = await db.storage.from("attachments").upload(path, file, { contentType: file.type || "image/png" });
  if (!error) {
    await db.from("attachments").insert({ trade_id: tradeId, storage_path: path });
  }
  revalidatePath(`/journal/${tradeId}`);
}

export async function deleteAttachment(id: string, tradeId: string) {
  const db = supabaseAdmin();
  const { data } = await db.from("attachments").select("storage_path").eq("id", id).maybeSingle();
  if (data) {
    await db.storage.from("attachments").remove([data.storage_path]);
    await db.from("attachments").delete().eq("id", id);
  }
  revalidatePath(`/journal/${tradeId}`);
}
