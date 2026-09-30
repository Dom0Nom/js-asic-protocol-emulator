// Scoped timing-independence claim for one lane (docs/verification.md).
//
// Two copies of tempo_lane see the same input pins and the same command
// tape, but each copy pushes the next tape entry at times chosen freely by
// the solver. The tape starts with a start time T and ends with an emit
// (which ends the frame). Claim: once both copies have begun the frame,
// their internal drive states and ownership are identical on every cycle,
// unless one of them has raised
// LATE (start time missed) or UNDER (ran out of commands inside the frame).
// Thread scheduling, stalls and host traffic only change *when* commands
// are pushed, so this is the property that makes pin timing independent of
// firmware timing for this constrained tape. This is not a completed
// proof or a check of the chip's pad resolver.

module lane_miter (input wire clk);
  localparam N = 6;  // tape length

  reg [15:0] now = 16'd0;
  reg        rst_n = 1'b0;
  always @(posedge clk) begin
    now <= now + 16'd1;
    rst_n <= 1'b1;
  end

  (* anyseq *) wire [15:0] pins_in;
  (* anyseq *) wire        go_a, go_b;

  // the command tape
  (* anyconst *) reg [3*N-1:0]  kind_v;
  (* anyconst *) reg [16*N-1:0] data_v;
  (* anyconst *) reg [5*N-1:0]  n_v;
  (* anyconst *) reg [3*N-1:0]  fl_v;
  (* anyconst *) reg [4*N-1:0]  rg_v;
  `define KIND(i) kind_v[(i)*3 +: 3]
  `define DATA(i) data_v[(i)*16 +: 16]
  `define NB(i)   n_v[(i)*5 +: 5]
  `define FL(i)   fl_v[(i)*3 +: 3]
  `define RG(i)   rg_v[(i)*4 +: 4]

  integer i;
  always @* begin
    // entry 0 turns FRAME on, entry 1 anchors the frame, the last entry ends it
    assume(`KIND(0) == 3'd3 && `RG(0) == 4'd15 && `DATA(0) == 16'h0008);
    assume(`KIND(1) == 3'd4 && `DATA(1) >= 16'd4 && `DATA(1) <= 16'd24);
    assume(`KIND(N-1) == 3'd2 && data_v[(N-1)*16 + 7]);   // end-of-frame emit
    for (i = 2; i < N - 1; i = i + 1) begin
      assume(`KIND(i) == 3'd0 || `KIND(i) == 3'd2 || `KIND(i) == 3'd3);  // tx, emit, lw
      if (`KIND(i) == 3'd2) assume(!data_v[i*16 + 7]);    // mid-frame emits
      assume(`NB(i) >= 5'd1 && `NB(i) <= 5'd4);
      assume(`FL(i)[0] == 1'b0);                   // no echo receive
      if (`KIND(i) == 3'd3) begin
        // keep slots short and prescaler off; FRAME stays on
        assume(`RG(i) != 4'd1 && `RG(i) != 4'd14);  // pins fixed, CAP read-only
        if (`RG(i) == 4'd2 || `RG(i) == 4'd3) assume(`DATA(i) >= 16'd1 && `DATA(i) <= 16'd3);
        if (`RG(i) == 4'd7 || `RG(i) == 4'd8 || `RG(i) == 4'd9) assume(`DATA(i) <= 16'd3);
        if (`RG(i) == 4'd4 || `RG(i) == 4'd5) assume(data_v[i*16 + 0 +: 13] <= 13'd3);
        if (`RG(i) == 4'd15) assume(data_v[i*16 + 0 +: 3] == 3'd0 && data_v[i*16 + 3] && data_v[i*16 + 7] == 1'b0 && data_v[i*16 + 9 +: 3] == 3'd0);
      end
    end
  end

  // each copy feeds the tape in order, whenever its own enable says so
  reg [2:0] pa = 3'd0, pb = 3'd0;
  wire      qfa, qfb;
  wire      push_a = rst_n && go_a && !qfa && pa < N;
  wire      push_b = rst_n && go_b && !qfb && pb < N;
  always @(posedge clk) begin
    if (push_a) pa <= pa + 3'd1;
    if (push_b) pb <= pb + 3'd1;
  end

  wire [15:0] st_a, st_b, rda, rdb, pc_a, pc_b, cap_a, cap_b, w_a, w_b;
  wire [1:0]  da, db, aa, ab, ca, cb;
  wire [2:0]  oa, ob;
  wire        ida, idb, rxa, rxb, ea, eb, era, erb, esa, esb, eea, eeb, runa, runb;

  tempo_lane #(.FULL(1)) A (
      .clk(clk), .rst_n(rst_n), .now(now), .pins_in(pins_in),
      .op_push(push_a), .op_kind(`KIND(pa)), .op_data(`DATA(pa)), .op_n(`NB(pa)), .op_flags(`FL(pa)),
      .op_reg(`RG(pa)), .op_pop(1'b0), .op_consume_start(1'b0), .op_consume_stop(1'b0),
      .op_clear(16'd0), .op_stop(1'b0), .rd_idx(4'd0), .rd_data(rda), .status(st_a), .q_full(qfa),
      .idle(ida), .rx_avail(rxa), .rx_word(w_a), .rx_end(ea), .rx_err(era), .ev_start_o(esa),
      .ev_stop_o(eea), .cap_o(cap_a), .pins_cfg(pc_a), .d_o(da), .a_o(aa), .c_o(ca), .owned_o(oa),
      .running_o(runa));
  tempo_lane #(.FULL(1)) B (
      .clk(clk), .rst_n(rst_n), .now(now), .pins_in(pins_in),
      .op_push(push_b), .op_kind(`KIND(pb)), .op_data(`DATA(pb)), .op_n(`NB(pb)), .op_flags(`FL(pb)),
      .op_reg(`RG(pb)), .op_pop(1'b0), .op_consume_start(1'b0), .op_consume_stop(1'b0),
      .op_clear(16'd0), .op_stop(1'b0), .rd_idx(4'd0), .rd_data(rdb), .status(st_b), .q_full(qfb),
      .idle(idb), .rx_avail(rxb), .rx_word(w_b), .rx_end(eb), .rx_err(erb), .ev_start_o(esb),
      .ev_stop_o(eeb), .cap_o(cap_b), .pins_cfg(pc_b), .d_o(db), .a_o(ab), .c_o(cb), .owned_o(ob),
      .running_o(runb));

  reg started_a = 1'b0, started_b = 1'b0;
  reg payload_skew_seen = 1'b0;
  reg driven_seen = 1'b0;
  reg payload_low_seen = 1'b0, payload_high_seen = 1'b0;
  wire payload_push_a = push_a && pa >= 3'd2 && pa <= 3'd4;
  wire payload_push_b = push_b && pb >= 3'd2 && pb <= 3'd4;
  wire has_tx = `KIND(2) == 3'd0 || `KIND(3) == 3'd0 || `KIND(4) == 3'd0;
  always @(posedge clk) begin
    if (rst_n && runa) started_a <= 1'b1;
    if (rst_n && runb) started_b <= 1'b1;
    if (rst_n && ((payload_push_a != payload_push_b) ||
                 (payload_push_a && payload_push_b && pa != pb)))
      payload_skew_seen <= 1'b1;
    if (rst_n && runa && runb && oa[0] && ob[0] && da == 2'b11 && db == 2'b11)
      driven_seen <= 1'b1;
    // Before either EOF is accepted, enabled low/high states cannot come
    // solely from the final emit. The stronger cover uses TX/LW/TX payload:
    // the intervening LW can configure a symbol before the second TX runs.
    if (rst_n && started_a && started_b && runa && runb && pa < N && pb < N &&
        oa[0] && ob[0] && da[1] && db[1]) begin
      if (!da[0] && !db[0]) payload_low_seen <= 1'b1;
      if ( da[0] &&  db[0]) payload_high_seen <= 1'b1;
    end
  end

`ifdef MITER_HINT
  // Witness-search hint only (cover_hint task): pin the tape to the one used
  // by check_miter_trace.py. It satisfies every tape assumption above, so a
  // cover reached here is reachable in the unconstrained miter too. Push
  // times and input pins stay free. Never use this define for BMC.
  always @* begin
    assume(kind_v == {3'd2, 3'd0, 3'd3, 3'd0, 3'd4, 3'd3});
    assume(data_v == {16'h0091, 16'd15, 16'hC000, 16'd0, 16'd20, 16'd8});
    assume(n_v == {5'd1, 5'd4, 5'd1, 5'd4, 5'd1, 5'd1});
    assume(fl_v == 18'd0);
    assume(rg_v == {4'd0, 4'd0, 4'd5, 4'd0, 4'd0, 4'd15});
  end
`endif

  wire bad_a = st_a[5] | st_a[6];  // LATE, UNDER
  wire bad_b = st_b[5] | st_b[6];
  always @* begin
    if (rst_n && started_a && started_b && !bad_a && !bad_b) begin
      assert(da == db);
      assert(aa == ab);
      assert(ca == cb);
      assert(oa == ob);
    end
    // Nonvacuity requires both executions to reach the driven frame and
    // finish the complete tape without excluding a timing failure. The
    // stronger witness also requires distinct payload enqueue schedules.
    cover(rst_n && started_a && started_b && has_tx && driven_seen &&
          pa == N && pb == N && ida && idb && !bad_a && !bad_b);
    cover(rst_n && started_a && started_b && driven_seen && payload_skew_seen &&
          `KIND(2) == 3'd0 && `KIND(3) == 3'd3 && `KIND(4) == 3'd0 &&
          payload_low_seen && payload_high_seen &&
          pa == N && pb == N && ida && idb && !bad_a && !bad_b);
  end
endmodule
