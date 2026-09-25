// Anchored start (docs/verification.md, use case 9): a command queued behind
// `start T` begins exactly at cycle T. The tape is: pins, slot length,
// start T, emit v. The solver pushes each entry whenever it likes (any
// thread timing). Claim: the emitted level appears on D exactly at cycle T,
// never earlier, unless the lane raised LATE (the emit arrived after T).
// This is what makes a triggered pulse's delay exact: the firmware computes
// T = CAP + delay, and the pin moves at T whatever the thread was doing.

module lane_anchor (input wire clk);
  reg [15:0] now = 16'd0;
  reg        rst_n = 1'b0;
  always @(posedge clk) begin
    now <= now + 16'd1;
    rst_n <= 1'b1;
  end

  (* anyseq *) wire [15:0] pins_in;
  (* anyseq *) wire        go;
  (* anyconst *) reg [15:0] T;
  (* anyconst *) reg        v;
  (* anyconst *) reg [1:0]  len;
  always @* begin
    assume(T >= 16'd6 && T <= 16'd36);
    assume(len != 2'd0);
  end

  reg  [2:0]  p = 3'd0;
  wire        qf;
  wire        push = rst_n && go && !qf && p < 3'd4;
  reg  [2:0]  kind;
  reg  [15:0] data;
  reg  [3:0]  rg;
  always @* begin
    case (p)
      3'd0: begin kind = 3'd3; rg = 4'd1; data = 16'hFFF0; end  // lw pins: D = pin 0
      3'd1: begin kind = 3'd3; rg = 4'd2; data = {14'd0, len}; end  // lw len0
      3'd2: begin kind = 3'd4; rg = 4'd0; data = T; end  // start T
      default: begin kind = 3'd2; rg = 4'd0; data = {11'd0, 1'b1, 3'd0, v}; end  // emit D = v
    endcase
  end
  always @(posedge clk) if (push) p <= p + 3'd1;

  wire [15:0] st, rd, pc, cap, w;
  wire [1:0]  d, a, c;
  wire [2:0]  o;
  wire        idle, rxa, e, er, es, ee, run;
  tempo_lane #(.FULL(1)) L (
      .clk(clk), .rst_n(rst_n), .now(now), .pins_in(pins_in),
      .op_push(push), .op_kind(kind), .op_data(data), .op_n(5'd1), .op_flags(3'd0),
      .op_reg(rg), .op_pop(1'b0), .op_consume_start(1'b0), .op_consume_stop(1'b0),
      .op_clear(16'd0), .op_stop(1'b0), .rd_idx(4'd0), .rd_data(rd), .status(st), .q_full(qf),
      .idle(idle), .rx_avail(rxa), .rx_word(w), .rx_end(e), .rx_err(er), .ev_start_o(es),
      .ev_stop_o(ee), .cap_o(cap), .pins_cfg(pc), .d_o(d), .a_o(a), .c_o(c), .owned_o(o),
      .running_o(run));

  wire late = st[5];
  wire hit = d == {1'b1, v} && o[0];  // the emitted level is on D
  reg  seen = 1'b0;                   // it has been there before
  reg  [15:0] done_at = 16'hFFFF;     // cycle the last tape entry was pushed
  always @(posedge clk) begin
    if (hit) seen <= 1'b1;
    if (push && p == 3'd3) done_at <= now;
  end
  always @* begin
    if (rst_n) begin
      // never early
      if (now < T) assert(!hit);
      // the first time the level appears, it is cycle T, or the lane says LATE
      if (hit && !seen) assert(now == T || late);
      // and it is on time whenever the emit was queued in time
      if (now == T && done_at <= T - 16'd2) assert(hit && !late);
    end
  end

  // the interesting cases are reachable: on time, and late
  always @* begin
    cover(rst_n && now == T && hit && !late);
    cover(rst_n && hit && !seen && late);
  end
endmodule
