import { redirect } from "next/navigation";

// Chat lives in the persistent dock (ChatDock in the root layout) now.
export default function ChatPage() {
  redirect("/today");
}
