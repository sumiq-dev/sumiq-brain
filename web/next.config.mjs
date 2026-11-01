// Production: a static export served by the Python harness (no Node.js server needed).
// Development (`npm run dev`): proxy /api to a running `brain harness`.
const dev = process.env.NODE_ENV === "development";
const harness = process.env.HARNESS_URL || "http://127.0.0.1:8780";

export default dev
  ? {
      compress: false, // keep server-sent events unbuffered
      async rewrites() {
        return [{ source: "/api/:path*", destination: `${harness}/api/:path*` }];
      },
    }
  : { output: "export", images: { unoptimized: true }, poweredByHeader: false };
