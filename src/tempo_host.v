/*
 * Host SPI port (mode 0). Works on synchronised pins; modelled by
 * tools/tempo/host.py. The byte shifted out during transaction byte k+1 is
 * chosen when byte k completes. MISO moves to the next bit on the same
 * (synchronised) rising SCK edge on which the host sampled the current one.
 *
 * Copyright (c) 2026 Dom0Nom
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

module tempo_host (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [15:0] pins_in,
    output wire        miso,

    input  wire [3:0]  run,
    input  wire [3:0]  illegal,
    input  wire [3:0]  late,
    input  wire [2:0]  t2h_count,
    input  wire [2:0]  h2t_count,
    input  wire [15:0] t2h_head,
    input  wire [6:0]  lane_flags,
    input  wire [31:0] lane_st,     // per lane {busy, STAT[9:3]}, lane 0 in [7:0]
    input  wire        any_err,     // some error flag is set (see HostPort.error)
    input  wire        pinconflict,
    input  wire [15:0] reg_val,
    input  wire [15:0] imem_rdata,

    // strobes valid in the cycle a byte completes; the core applies them at
    // the same clock edge (after the thread commit, so the host wins)
    output wire        ctl_v,
    output wire [7:0]  ctl,
    output wire        pc_v,
    output wire [1:0]  pc_t,
    output wire [9:0]  pc_val,
    output wire        h2t_v,
    output wire [15:0] h2t_word,
    output wire        t2h_pop,
    output wire        step_v,
    output wire [1:0]  step_t,
    output wire [1:0]  reg_t,
    output wire [3:0]  reg_r,

    output reg         wr_pend,
    output reg  [9:0]  wr_addr,
    output reg  [15:0] wr_data,
    input  wire        wr_done,
    output reg         rd_pend,
    output reg  [9:0]  rd_addr,
    input  wire        rd_done
);
  wire cs   = !pins_in[15];
  wire sck  = pins_in[13];  // ui[5], RP2350 SPI0 SCK on the demo board
  wire mosi = pins_in[14];  // ui[6], SPI0 TX

  reg        cs_active, sck_prev;
  reg [2:0]  bitcnt;
  reg [7:0]  sh_in, mo, idx, cmd, hi;
  reg [9:0]  addr;
  reg [1:0]  thread;
  reg [3:0]  rreg;
  reg [15:0] word;
  reg [2:0]  rcnt;      // words a mailbox read may still pop, or a write push
  reg        pk;        // mailbox read: the head word is being shifted out

  assign miso = mo[7];
  assign reg_t = thread;
  assign reg_r = rreg;

  wire       rise = sck && !sck_prev;
  wire [7:0] b = {sh_in[6:0], mosi};
  wire       byte_done = cs && cs_active && rise && bitcnt == 3'd7;
  wire [2:0] bitcnt_first = 3'd0;  // bit counter after CS falls

  // byte handler (HostPort.byte): the next MISO byte and side effects
  reg [7:0]  out;
  reg        n_ctl_v, n_pc_v, n_h2t_v, n_pop, n_step_v, n_wr, n_rd;
  reg [9:0]  n_addr, n_rd_addr;
  reg [7:0]  n_hi;
  reg [1:0]  n_thread;
  reg [3:0]  n_rreg;
  reg [15:0] n_word;
  reg [2:0]  n_rcnt;
  reg        n_pk;
  always @* begin
    out = 8'd0;
    n_ctl_v = 1'b0; n_pc_v = 1'b0; n_h2t_v = 1'b0; n_pop = 1'b0; n_step_v = 1'b0; n_wr = 1'b0; n_rd = 1'b0;
    n_addr = addr; n_rd_addr = addr; n_hi = hi; n_thread = thread; n_rreg = rreg; n_word = word;
    n_rcnt = rcnt; n_pk = pk;
    if (idx == 8'd0) begin
      // mailbox read: the byte after the command carries the T2H level, and
      // the read pops at most that many words
      if (b == 8'h09) begin out = {any_err, 4'd0, t2h_count}; n_rcnt = t2h_count; n_pk = 1'b0; end
      // mailbox write: the free H2T space; at most that many words are pushed
      if (b == 8'h08) begin out = {any_err, 4'd0, 3'd4 - h2t_count}; n_rcnt = 3'd4 - h2t_count; end
    end else begin
      case (cmd)
        8'h01, 8'h02: begin
          if (idx == 8'd1) n_hi = b;
          else if (idx == 8'd2) begin
            n_addr = {hi[1:0], b};
            if (cmd == 8'h02) begin n_rd = 1'b1; n_rd_addr = {hi[1:0], b}; end
          end else if (cmd == 8'h01) begin
            if (idx[0]) n_hi = b;            // bytes 3, 5, ...: high byte
            else begin n_wr = 1'b1; n_addr = addr + 10'd1; end
          end else begin
            if (idx[0]) out = imem_rdata[15:8];
            else begin
              out = imem_rdata[7:0];
              n_addr = addr + 10'd1;
              n_rd = 1'b1; n_rd_addr = addr + 10'd1;
            end
          end
        end
        8'h05: if (idx == 8'd1) n_ctl_v = 1'b1;
        8'h06: begin
          if (idx == 8'd1) n_thread = b[1:0];
          else if (idx == 8'd2) n_hi = b;
          else if (idx == 8'd3 && !run[thread]) n_pc_v = 1'b1;
        end
        8'h07: begin
          case (idx)
            8'd1: out = {illegal, run};
            8'd2: out = {1'b0, t2h_count, 1'b0, h2t_count};
            8'd3: out = {pinconflict, lane_flags};
            8'd4: out = {4'd0, late};
            8'd5: out = lane_st[7:0];
            8'd6: out = lane_st[15:8];
            8'd7: out = lane_st[23:16];
            8'd8: out = lane_st[31:24];
            default: out = 8'd0;
          endcase
        end
        8'h08: begin
          if (idx[0]) n_hi = b;
          else if (rcnt != 3'd0) begin n_h2t_v = 1'b1; n_rcnt = rcnt - 3'd1; end
        end
        8'h09: begin
          // peek when the byte before the high byte completes, pop once the
          // high byte has gone out: a host that stops after n words loses none
          if (idx[0]) begin
            n_pk = (rcnt != 3'd0);
            n_word = n_pk ? t2h_head : 16'd0;
            out = n_word[15:8];
          end else begin
            out = word[7:0];
            if (pk) begin n_pop = 1'b1; n_rcnt = rcnt - 3'd1; n_pk = 1'b0; end
          end
        end
        8'h0A: if (idx == 8'd1 && !run[b[1:0]]) n_step_v = 1'b1;
        8'h0B: begin
          if (idx == 8'd1) n_thread = b[1:0];
          else if (idx == 8'd2) n_rreg = b[3:0];
          else if (idx == 8'd3) begin n_word = reg_val; out = reg_val[15:8]; end
          else if (idx == 8'd4) out = word[7:0];
        end
        default: ;
      endcase
    end
  end

  always @(posedge clk) begin
    if (!rst_n) begin
      cs_active <= 1'b0; sck_prev <= 1'b0; bitcnt <= 3'd0; sh_in <= 8'd0; mo <= 8'd0; idx <= 8'd0;
      cmd <= 8'd0; hi <= 8'd0; addr <= 10'd0; thread <= 2'd0; rreg <= 4'd0; word <= 16'd0;
      rcnt <= 3'd0; pk <= 1'b0;
      wr_pend <= 1'b0; wr_addr <= 10'd0; wr_data <= 16'd0; rd_pend <= 1'b0; rd_addr <= 10'd0;
    end else begin
      if (wr_done) wr_pend <= 1'b0;
      if (rd_done) rd_pend <= 1'b0;
      sck_prev <= sck;
      if (!cs) begin
        cs_active <= 1'b0;
      end else begin
        if (!cs_active) begin
          cs_active <= 1'b1;
          mo <= 8'd0;
        end
        if (rise) begin
          sh_in <= b;
          bitcnt <= (cs_active ? bitcnt : bitcnt_first) + 3'd1;
          if ((cs_active ? bitcnt : bitcnt_first) == 3'd7) begin
            mo <= out;
            idx <= (cs_active ? idx : 8'd0) == 8'd255 ? 8'd128 : (cs_active ? idx : 8'd0) + 8'd1;  // keep parity
            if (!cs_active || idx == 8'd0) cmd <= b;
            hi <= n_hi; addr <= n_addr; thread <= n_thread; rreg <= n_rreg; word <= n_word;
            rcnt <= n_rcnt; pk <= n_pk;
            if (n_wr) begin wr_pend <= 1'b1; wr_addr <= addr; wr_data <= {hi, b}; end
            if (n_rd) begin rd_pend <= 1'b1; rd_addr <= n_rd_addr; end
          end else if (cs_active) begin
            mo <= {mo[6:0], 1'b0};
          end
        end
        if (!cs_active) begin
          bitcnt <= rise ? 3'd1 : 3'd0;
          if (!rise) idx <= 8'd0;
        end
      end
    end
  end


  assign ctl_v    = byte_done && n_ctl_v;
  assign ctl      = b;
  assign pc_v     = byte_done && n_pc_v;
  assign pc_t     = thread;
  assign pc_val   = {hi[1:0], b};
  assign h2t_v    = byte_done && n_h2t_v;
  assign h2t_word = {hi, b};
  assign t2h_pop  = byte_done && n_pop;
  assign step_v   = byte_done && n_step_v;
  assign step_t   = b[1:0];
`ifdef FORMAL
  // induction helpers for the core's mailbox bounds (formal/core.sby): a
  // write frame pushes at most the space it reported, a read pops at most
  // the level it reported
  reg f_past = 1'b0;
  always @(posedge clk) f_past <= 1'b1;
  always @(posedge clk) begin
    if (f_past && rst_n && $past(rst_n) && idx != 8'd0) begin
      if (cmd == 8'h08) assert({1'b0, rcnt} + {1'b0, h2t_count} <= 4'd4);
      if (cmd == 8'h09) assert(rcnt <= t2h_count && (!pk || rcnt != 3'd0));
    end
  end
`endif
endmodule
