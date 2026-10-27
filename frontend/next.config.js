const API = process.env.API_URL || "http://127.0.0.1:8000";
module.exports = {
  output: "standalone",
  compress: false, // gzip của Next giữ đệm luồng SSE (/api/stream) nên trình duyệt không nhận được dữ liệu
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API}/api/:path*` }];
  },
};
