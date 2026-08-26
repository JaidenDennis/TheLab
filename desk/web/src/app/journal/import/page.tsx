import { ImportDrop } from "@/components/ImportDrop";

export default function ImportPage() {
  return (
    <>
      <h1>Import fills</h1>
      <p className="muted">
        Tradovate → Reports → Fills → export CSV, then drop it here. Importing the same file twice changes nothing.
      </p>
      <ImportDrop />
    </>
  );
}
