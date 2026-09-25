/*
 * Tempo core: four-thread barrel datapath, lanes, mailboxes, host port and
 * pin resolution. The SRAM, input synchroniser and output register live in
 * the top level. Modelled cycle for cycle by tools/tempo/sim.py.
 *
 * Pipeline, for a thread that executes in cycle e (NOW mod 4 = thread):
 *   e-2  fetch: IMEM address = PC (or the ldi data address)
 *   e-1  SRAM data out, captured into IR
 *   e    execute: read registers, check, commit everything at the clock edge
 *
 * Copyright (c) 2026 Dom0Nom
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

module tempo_core (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [15:0] pins_in,     // synchronised inputs, index order of the spec

    output wire [9:0]  imem_addr,
    output wire        imem_ren,
    output wire        imem_wen,
    output wire [15:0] imem_din,
    input  wire [15:0] imem_dout,

    output reg  [15:0] out_val,     // resolved pad values for the next cycle
    output reg  [15:0] out_oe,

    // debug and lockstep visibility (not connected to pins)
    output wire [15:0] now_o,
    output reg         commit_o,
    output reg  [1:0]  commit_tid,
    output reg  [9:0]  commit_pc,
    output reg         commit_wr,
    output reg  [2:0]  commit_rd,
    output reg  [15:0] commit_val
);
`include "tempo_isa.vh"

  // ------------------------------------------------------------ state
  reg [15:0] now;
  reg [9:0]  pc [0:3];
  reg [15:0] regs [0:31];
  reg [3:0]  fz, fc, fn, late, run, step, illegal, ldi_phase;
  reg [9:0]  ldi_addr [0:3];
  reg [2:0]  ldi_rd [0:3];
  reg        fv1, irv;
  reg [15:0] ir;
  reg [15:0] scratch [0:7];
  reg [15:0] gout, goe;
  reg [7:0]  ev;
  reg [15:0] h2t [0:3];
  reg [15:0] t2h [0:3];
  reg [2:0]  h2t_n, t2h_n;
  reg        pinconflict;

  assign now_o = now;

  // ------------------------------------------------------------ fetch
  wire [1:0] ft = now[1:0] + 2'd2;
  wire [1:0] te = now[1:0];
  wire       f_run = run[ft];

  // host IMEM requests, granted in fetch slots of halted threads
  wire        h_wr_pend, h_rd_pend;
  wire [9:0]  h_wr_addr, h_rd_addr;
  wire [15:0] h_wr_data;
  wire        h_grant_wr = !f_run && h_wr_pend;
  wire        h_grant_rd = !f_run && !h_wr_pend && h_rd_pend;
  reg         h_rd_capture;

  assign imem_addr = f_run ? (ldi_phase[ft] ? ldi_addr[ft] : pc[ft]) :
                     (h_grant_wr ? h_wr_addr : h_rd_addr);
  assign imem_ren  = f_run || h_grant_rd;
  assign imem_wen  = h_grant_wr;
  assign imem_din  = h_wr_data;

  // ------------------------------------------------------------ decode
  wire [3:0] op  = ir[15:12];
  wire [2:0] fa  = ir[11:9];
  wire [2:0] fb  = ir[8:6];
  wire [2:0] fc3 = ir[5:3];
  wire [5:0] fn6 = ir[5:0];
  wire [1:0] ln  = ir[11:10];
  wire [3:0] lreg = ir[9:6];
  wire [8:0] imm9 = ir[8:0];
  wire [15:0] simm9 = {{7{ir[8]}}, ir[8:0]};

  wire use_c = (op == OP_ALU3) || (op == OP_LREG) || (op == OP_TX) || (op == OP_LANE);
  wire [2:0] xi = use_c ? fc3 : fa;
  wire [15:0] rx_ = regs[{te, xi}];   // port X: [11:9] or [5:3]
  wire [15:0] ry  = regs[{te, fb}];   // port Y: [8:6]
  wire [2:0] saddr = ry[2:0] + ir[2:0];  // scratch address, mod 8

  wire illegal_insn =
      (op == OP_ALU2 && fn6[5:4] == 2'b00 && fn6[3]) ||
      (op == OP_J && ir[10]) ||
      (op == OP_SYS && fn6 > SYS_TSTAT) ||
      (op == OP_LREG && ir[1:0] != 2'b00);

  // ------------------------------------------------------------ lanes
  wire [15:0] l_rd [0:3];
  wire [15:0] l_status [0:3];
  wire [3:0]  l_qfull, l_idle, l_rxav, l_rxend, l_rxerr, l_evs, l_eve;
  wire [15:0] l_rxword [0:3];
  wire [15:0] l_cap [0:3];
  wire [15:0] l_pins [0:3];
  wire [1:0]  l_d [0:3];
  wire [1:0]  l_a [0:3];
  wire [1:0]  l_c [0:3];
  wire [2:0]  l_owned [0:3];

  reg         lo_push, lo_pop, lo_cs, lo_ce, lo_stop;
  reg [2:0]   lo_kind, lo_flags;
  reg [15:0]  lo_data, lo_clear;
  reg [4:0]   lo_n;
  reg [3:0]   lo_reg;

  reg         ex_valid, ex_commit, ex_commit_lane, stall;
  reg         wr_en;
  reg  [2:0]  wr_idx;
  reg  [15:0] wr_val;
  reg  [9:0]  nxt;
  reg         set_z, set_n, set_c;
  reg  [15:0] nz_val;
  reg         c_val;
  reg  [16:0] sum;
  reg  [15:0] res, a, b;
  reg  [3:0]  sh;
  reg         cond_ok;
  reg  [15:0] ready;
  reg  [15:0] tdiff;
  reg  [1:0]  li;
  integer     q;

  function [15:0] brev16;
    input [15:0] v;
    integer k;
    for (k = 0; k < 16; k = k + 1) brev16[k] = v[15 - k];
  endfunction
  reg         pc_conf;

  genvar gi;
  generate
    for (gi = 0; gi < 4; gi = gi + 1) begin : g_lane
      wire sel = (ln == gi) && ex_commit_lane;
      tempo_lane #(.FULL(gi < 2 ? 1 : 0)) lane (
          .clk(clk), .rst_n(rst_n), .now(now), .pins_in(pins_in),
          .op_push(sel && lo_push), .op_kind(lo_kind), .op_data(lo_data), .op_n(lo_n),
          .op_flags(lo_flags), .op_reg(lo_reg), .op_pop(sel && lo_pop),
          .op_consume_start(sel && lo_cs), .op_consume_stop(sel && lo_ce),
          .op_clear(sel ? lo_clear : 16'd0), .op_stop(sel && lo_stop),
          .rd_idx(lreg), .rd_data(l_rd[gi]), .status(l_status[gi]), .q_full(l_qfull[gi]),
          .idle(l_idle[gi]), .rx_avail(l_rxav[gi]), .rx_word(l_rxword[gi]), .rx_end(l_rxend[gi]),
          .rx_err(l_rxerr[gi]), .ev_start_o(l_evs[gi]), .ev_stop_o(l_eve[gi]), .cap_o(l_cap[gi]),
          .pins_cfg(l_pins[gi]), .d_o(l_d[gi]), .a_o(l_a[gi]), .c_o(l_c[gi]), .owned_o(l_owned[gi]),
          .running_o()
      );
    end
  endgenerate

  // ------------------------------------------------------------ host port
  wire        hp_miso;
  wire        hp_ctl_v;
  wire [7:0]  hp_ctl;
  wire        hp_pc_v;
  wire [1:0]  hp_pc_t;
  wire [9:0]  hp_pc;
  wire        hp_h2t_v;
  wire [15:0] hp_h2t;
  wire        hp_t2h_pop;
  wire        hp_step_v;
  wire [1:0]  hp_step_t;
  wire [1:0]  hp_rt;
  wire [3:0]  hp_rr;
  reg  [15:0] hp_rval;
  reg  [15:0] hp_rdata;
  wire [6:0]  lane_flags = l_status[0][9:3] | l_status[1][9:3] | l_status[2][9:3] | l_status[3][9:3];

  always @* begin
    if (run[hp_rt]) hp_rval = 16'd0;
    else if (hp_rr < 4'd8) hp_rval = regs[{hp_rt, hp_rr[2:0]}];
    else if (hp_rr == 4'd8) hp_rval = {6'd0, pc[hp_rt]};
    else hp_rval = {13'd0, fn[hp_rt], fc[hp_rt], fz[hp_rt]};
  end

  tempo_host host (
      .clk(clk), .rst_n(rst_n), .pins_in(pins_in), .miso(hp_miso),
      .run(run), .illegal(illegal), .late(late), .t2h_count(t2h_n), .h2t_count(h2t_n),
      .t2h_head(t2h[0]), .lane_flags(lane_flags), .pinconflict(pinconflict),
      .reg_val(hp_rval), .imem_rdata(hp_rdata),
      .ctl_v(hp_ctl_v), .ctl(hp_ctl), .pc_v(hp_pc_v), .pc_t(hp_pc_t), .pc_val(hp_pc),
      .h2t_v(hp_h2t_v), .h2t_word(hp_h2t), .t2h_pop(hp_t2h_pop), .step_v(hp_step_v), .step_t(hp_step_t),
      .reg_t(hp_rt), .reg_r(hp_rr),
      .wr_pend(h_wr_pend), .wr_addr(h_wr_addr), .wr_data(h_wr_data), .wr_done(h_grant_wr),
      .rd_pend(h_rd_pend), .rd_addr(h_rd_addr), .rd_done(h_grant_rd)
  );

  // ------------------------------------------------------------ execute

  always @* begin
    ex_valid = irv && run[te];
    ex_commit = 1'b0;
    ex_commit_lane = 1'b0;
    stall = 1'b0;
    wr_en = 1'b0; wr_idx = fa; wr_val = 16'd0;
    nxt = pc[te] + 10'd1;
    set_z = 1'b0; set_n = 1'b0; set_c = 1'b0; nz_val = 16'd0; c_val = fc[te];
    sum = 17'd0; res = 16'd0; a = 16'd0; b = 16'd0; sh = 4'd0; cond_ok = 1'b0; ready = 16'd0; tdiff = 16'd0;
    lo_push = 1'b0; lo_pop = 1'b0; lo_cs = 1'b0; lo_ce = 1'b0; lo_stop = 1'b0;
    lo_kind = 3'd0; lo_flags = 3'd0; lo_data = 16'd0; lo_clear = 16'd0; lo_n = 5'd0; lo_reg = 4'd0;
    li = ln;

    if (ex_valid && ldi_phase[te]) begin
      ex_commit = 1'b1;
      wr_en = 1'b1; wr_idx = ldi_rd[te]; wr_val = ir;
    end else if (ex_valid && !illegal_insn) begin
      ex_commit = 1'b1;
      case (op)
        OP_ALU3: begin
          a = ry; b = rx_; sh = b[3:0];
          case (ir[2:0])
            ALU3_ADD: begin sum = {1'b0, a} + {1'b0, b}; res = sum[15:0]; set_c = 1'b1; c_val = sum[16]; end
            ALU3_SUB: begin res = a - b; set_c = 1'b1; c_val = (a >= b); end
            ALU3_AND: res = a & b;
            ALU3_OR:  res = a | b;
            ALU3_XOR: res = a ^ b;
            ALU3_SHL: begin res = a << sh; if (sh != 4'd0) begin set_c = 1'b1; c_val = a[5'd16 - {1'b0, sh}]; end end
            ALU3_SHR: begin res = a >> sh; if (sh != 4'd0) begin set_c = 1'b1; c_val = a[sh - 4'd1]; end end
            default:  begin res = $signed(a) >>> sh; if (sh != 4'd0) begin set_c = 1'b1; c_val = a[sh - 4'd1]; end end
          endcase
          wr_en = 1'b1; wr_val = res; set_z = 1'b1; set_n = 1'b1; nz_val = res;
        end
        OP_ALU2: begin
          a = ry; sh = fn6[3:0];
          wr_en = 1'b1; set_z = 1'b1; set_n = 1'b1;
          if (fn6[5:4] == 2'b01) begin
            res = a << sh; if (sh != 4'd0) begin set_c = 1'b1; c_val = a[5'd16 - {1'b0, sh}]; end
          end else if (fn6[5:4] == 2'b10) begin
            res = a >> sh; if (sh != 4'd0) begin set_c = 1'b1; c_val = a[sh - 4'd1]; end
          end else if (fn6[5:4] == 2'b11) begin
            res = (a >> sh) | (a << (5'd16 - {1'b0, sh}));
          end else begin
            case (fn6[2:0])
              ALU2_MOV[2:0]: begin res = a; set_z = 1'b0; set_n = 1'b0; end
              ALU2_NOT[2:0]: res = ~a;
              ALU2_NEG[2:0]: res = -a;
              ALU2_CMP[2:0]: begin wr_en = 1'b0; nz_val = rx_ - a; set_c = 1'b1; c_val = (rx_ >= a); end
              ALU2_TST[2:0]: begin wr_en = 1'b0; nz_val = rx_ & a; end
              ALU2_PAR[2:0]: res = {15'd0, ^a};
              ALU2_BREV[2:0]: res = brev16(a);
              default: res = {a[7:0], a[15:8]};
            endcase
          end
          wr_val = res;
          if (wr_en) nz_val = res;
        end
        OP_LI: begin wr_en = 1'b1; wr_val = simm9; end
        OP_ADDI: begin
          sum = {1'b0, rx_} + {1'b0, simm9};
          wr_en = 1'b1; wr_val = sum[15:0]; set_z = 1'b1; set_n = 1'b1; nz_val = sum[15:0]; set_c = 1'b1; c_val = sum[16];
        end
        OP_CMPI: begin
          set_z = 1'b1; set_n = 1'b1; nz_val = rx_ - simm9; set_c = 1'b1; c_val = (rx_ >= simm9);
        end
        OP_ANDI: begin
          wr_en = 1'b1; wr_val = rx_ & simm9; set_z = 1'b1; set_n = 1'b1; nz_val = rx_ & simm9;
        end
        OP_IMM8: begin
          wr_en = 1'b1;
          if (!ir[8]) wr_val = {ir[7:0], rx_[7:0]};
          else begin
            wr_val = ir[7] ? (rx_ ^ {9'd0, ir[6:0]}) : (rx_ | {9'd0, ir[6:0]});
            set_z = 1'b1; set_n = 1'b1; nz_val = wr_val;
          end
        end
        OP_MEM: begin
          if (ir[5]) begin
            // st: handled in the sequential block
          end else begin
            wr_en = 1'b1; wr_val = scratch[saddr];
          end
        end
        OP_B: begin
          case (fa)
            COND_AL: cond_ok = 1'b1;
            COND_EQ: cond_ok = fz[te];
            COND_NE: cond_ok = !fz[te];
            COND_CS: cond_ok = fc[te];
            COND_CC: cond_ok = !fc[te];
            COND_MI: cond_ok = fn[te];
            COND_PL: cond_ok = !fn[te];
            default: cond_ok = fc[te] && !fz[te];
          endcase
          if (cond_ok) nxt = pc[te] + 10'd1 + {simm9[9:0]};
        end
        OP_DJNZ: begin
          wr_en = 1'b1; wr_val = rx_ - 16'd1;
          if (wr_val != 16'd0) nxt = pc[te] + 10'd1 + {simm9[9:0]};
        end
        OP_J: begin
          if (ir[11]) begin wr_en = 1'b1; wr_idx = 3'd7; wr_val = {6'd0, pc[te] + 10'd1}; end
          nxt = ir[9:0];
        end
        OP_LDI: begin
          // first slot: the thread's next fetch reads the data word
          ex_commit = 1'b0;
        end
        OP_SYS: begin
          case (fn6)
            SYS_NOP: ;
            SYS_HALT: ;
            SYS_JR: nxt = rx_[9:0];
            SYS_NOW: begin wr_en = 1'b1; wr_val = now; end
            SYS_WAITT: begin tdiff = now - rx_; if (tdiff[15]) stall = 1'b1; end
            SYS_IOR: begin wr_en = 1'b1; wr_val = pins_in; end
            SYS_IOW: ;
            SYS_IOE: ;
            SYS_HPUT: if (t2h_n == 3'd4) stall = 1'b1;
            SYS_HGET: begin if (h2t_n == 3'd0) stall = 1'b1; else begin wr_en = 1'b1; wr_val = h2t[0]; end end
            SYS_SEV: ;
            SYS_WEV: if (!ev[fb]) stall = 1'b1;
            SYS_TID: begin wr_en = 1'b1; wr_val = {14'd0, te}; end
            SYS_HCNT: begin wr_en = 1'b1; wr_val = {5'd0, 3'd4 - t2h_n, 5'd0, h2t_n}; end
            SYS_WANY: begin
              ready = {ev[5:0], (t2h_n != 3'd4), (h2t_n != 3'd0), l_evs, l_rxav} & ry;
              tdiff = now - rx_;
              if (ready == 16'd0 && tdiff[15]) stall = 1'b1;
              else begin wr_en = 1'b1; wr_val = ready; end
            end
            default: begin wr_en = 1'b1; wr_val = {15'd0, late[te]}; end  // tstat
          endcase
        end
        OP_LREG: begin
          if (!ir[2]) begin
            wr_en = 1'b1; wr_idx = fc3; wr_val = l_rd[li];
          end else if (l_qfull[li]) stall = 1'b1;
          else begin
            ex_commit_lane = 1'b1; lo_push = 1'b1; lo_kind = 3'd3; lo_data = rx_; lo_reg = lreg;
          end
        end
        OP_TX: begin
          if (l_qfull[li]) stall = 1'b1;
          else begin
            ex_commit_lane = 1'b1; lo_push = 1'b1; lo_kind = 3'd0; lo_data = rx_;
            lo_n = {1'b0, ir[9:6]} + 5'd1; lo_flags = ir[2:0];
          end
        end
        default: begin  // OP_LANE
          case (ir[2:0])
            LANE_RX: begin
              if (!l_rxav[li]) stall = 1'b1;
              else begin
                ex_commit_lane = 1'b1; lo_pop = 1'b1;
                wr_en = 1'b1; wr_idx = fc3; wr_val = l_rxword[li];
              end
            end
            LANE_RXN, LANE_EMIT, LANE_START, LANE_SYNC: begin
              if (l_qfull[li]) stall = 1'b1;
              else begin
                ex_commit_lane = 1'b1; lo_push = 1'b1;
                case (ir[2:0])
                  LANE_RXN:  begin lo_kind = 3'd1; lo_n = {1'b0, ir[9:6]} + 5'd1; lo_flags = {2'd0, ir[3]}; end
                  LANE_EMIT: begin lo_kind = 3'd2; lo_data = rx_; end
                  LANE_START: begin lo_kind = 3'd4; lo_data = rx_; end
                  default:   begin lo_kind = 3'd4; lo_data = l_cap[li] + rx_; end
                endcase
              end
            end
            LANE_WAIT: begin
              case (ir[9:6])
                4'd0: if (!l_idle[li]) stall = 1'b1;
                4'd1: begin if (!l_evs[li]) stall = 1'b1; else begin ex_commit_lane = 1'b1; lo_cs = 1'b1; end end
                4'd2: begin if (!l_eve[li]) stall = 1'b1; else begin ex_commit_lane = 1'b1; lo_ce = 1'b1; end end
                default: if (!l_rxav[li]) stall = 1'b1;
              endcase
            end
            LANE_STOP: begin ex_commit_lane = 1'b1; lo_stop = 1'b1; end
            default: begin  // stat
              ex_commit_lane = 1'b1;
              wr_en = 1'b1; wr_idx = fc3; wr_val = l_status[li];
              lo_clear = l_status[li] & 16'h03F8;
            end
          endcase
        end
      endcase
      if (stall) begin
        ex_commit = 1'b0;
        ex_commit_lane = 1'b0;
        wr_en = 1'b0;
      end
      if (op == OP_LDI) wr_en = 1'b0;
    end
  end

  // ------------------------------------------------------------ pin resolution
  reg [15:0] rv, ro, seen, lv, lo, lown;
  integer    li2;
  always @* begin
    rv = {hp_miso, gout[14:0]};
    ro = goe | 16'hFF00;
    seen = 16'd0;
    pc_conf = 1'b0;
    for (li2 = 3; li2 >= 0; li2 = li2 - 1) begin
      lv = 16'd0; lo = 16'd0; lown = 16'd0;
      if (l_pins[li2][3:0] != 4'd15 && l_owned[li2][0]) begin
        lown[l_pins[li2][3:0]] = 1'b1; lv[l_pins[li2][3:0]] = l_d[li2][0]; lo[l_pins[li2][3:0]] = l_d[li2][1];
      end
      if (l_pins[li2][15:12] != 4'd15 && l_owned[li2][1]) begin
        lown[l_pins[li2][15:12]] = 1'b1; lv[l_pins[li2][15:12]] = l_a[li2][0]; lo[l_pins[li2][15:12]] = l_a[li2][1];
      end
      if (l_pins[li2][11:8] != 4'd15 && l_owned[li2][2]) begin
        lown[l_pins[li2][11:8]] = 1'b1; lv[l_pins[li2][11:8]] = l_c[li2][0]; lo[l_pins[li2][11:8]] = l_c[li2][1];
      end
      if ((seen & lown) != 16'd0) pc_conf = 1'b1;
      seen = seen | lown;
      rv = (rv & ~lown) | (lv & lown);
      ro = (ro & ~lown) | (lo & lown);
    end
  end

  // ------------------------------------------------------------ sequential
  always @(posedge clk) begin
    if (!rst_n) begin
      now <= 16'd0;
      fz <= 0; fc <= 0; fn <= 0; late <= 0; run <= 0; step <= 0; illegal <= 0; ldi_phase <= 0;
      for (q = 0; q < 4; q = q + 1) begin
        pc[q] <= 10'd0; ldi_addr[q] <= 10'd0; ldi_rd[q] <= 3'd0; h2t[q] <= 16'd0; t2h[q] <= 16'd0;
      end
      for (q = 0; q < 32; q = q + 1) begin
        regs[q] <= 16'd0;
        if (q < 8) scratch[q[2:0]] <= 16'd0;
      end
      fv1 <= 1'b0; irv <= 1'b0; ir <= 16'd0;
      gout <= 16'd0; goe <= 16'd0; ev <= 8'd0; h2t_n <= 3'd0; t2h_n <= 3'd0;
      pinconflict <= 1'b0; out_val <= 16'd0; out_oe <= 16'h0000;
      h_rd_capture <= 1'b0; hp_rdata <= 16'd0;
      commit_o <= 1'b0; commit_tid <= 2'd0; commit_pc <= 10'd0; commit_wr <= 1'b0; commit_rd <= 3'd0; commit_val <= 16'd0;
    end else begin
      now <= now + 16'd1;
      // fetch pipeline
      fv1 <= f_run;
      irv <= fv1;
      ir <= imem_dout;
      h_rd_capture <= h_grant_rd;
      if (h_rd_capture) hp_rdata <= imem_dout;

      // pads for the next cycle
      out_val <= rv;
      out_oe <= ro;
      if (pc_conf) pinconflict <= 1'b1;

      // execute commit
      commit_o <= ex_commit;
      commit_tid <= te;
      commit_pc <= pc[te];
      commit_wr <= wr_en;
      commit_rd <= wr_idx;
      commit_val <= wr_val;
      if (ex_valid && illegal_insn && !ldi_phase[te]) begin
        run[te] <= 1'b0;
        illegal[te] <= 1'b1;
        step[te] <= 1'b0;
      end
      if (ex_valid && !ldi_phase[te] && !illegal_insn && op == OP_LDI) begin
        ldi_phase[te] <= 1'b1;
        ldi_addr[te] <= ry[9:0] + {4'd0, ir[5:0]};
        ldi_rd[te] <= fa;
      end
      if (ex_commit) begin
        if (wr_en) regs[{te, wr_idx}] <= wr_val;
        if (set_z) fz[te] <= (nz_val == 16'd0);
        if (set_n) fn[te] <= nz_val[15];
        if (set_c) fc[te] <= c_val;
        if (ldi_phase[te]) ldi_phase[te] <= 1'b0;
        pc[te] <= nxt;
        if (op == OP_SYS && fn6 == SYS_HALT && !ldi_phase[te]) run[te] <= 1'b0;
        if (step[te]) begin run[te] <= 1'b0; step[te] <= 1'b0; end
        if (op == OP_SYS && fn6 == SYS_WAITT && !ldi_phase[te] && tdiff >= 16'd4) late[te] <= 1'b1;
        if (op == OP_SYS && fn6 == SYS_TSTAT && !ldi_phase[te]) late[te] <= 1'b0;
        if (op == OP_SYS && fn6 == SYS_IOW && !ldi_phase[te]) gout <= (gout & ~ry) | (rx_ & ry);
        if (op == OP_SYS && fn6 == SYS_IOE && !ldi_phase[te]) goe <= (goe & ~ry) | (rx_ & ry);
        if (op == OP_SYS && fn6 == SYS_SEV && !ldi_phase[te]) ev[fb] <= 1'b1;
        if (op == OP_SYS && fn6 == SYS_WEV && !ldi_phase[te]) ev[fb] <= 1'b0;
        if (op == OP_MEM && ir[5] && !ldi_phase[te]) scratch[saddr] <= rx_;
        if (op == OP_LANE && ir[2:0] == LANE_RX && !ldi_phase[te]) begin
          fc[te] <= l_rxend[li]; fn[te] <= l_rxerr[li]; fz[te] <= (l_rxword[li] == 16'd0);
        end
      end

      // mailboxes: thread side (from the execute commit) then host side
      begin : mbox
        reg [2:0] hn, tn;
        hn = h2t_n; tn = t2h_n;
        if (ex_commit && !ldi_phase[te] && op == OP_SYS && fn6 == SYS_HGET) begin
          h2t[0] <= h2t[1]; h2t[1] <= h2t[2]; h2t[2] <= h2t[3];
          hn = hn - 3'd1;
        end
        if (hp_h2t_v) begin
          // host checked "not full" on the pre-edge count
          if (ex_commit && !ldi_phase[te] && op == OP_SYS && fn6 == SYS_HGET) h2t[h2t_n - 3'd1] <= hp_h2t;
          else h2t[h2t_n[1:0]] <= hp_h2t;
          hn = hn + 3'd1;
        end
        if (hp_t2h_pop) begin
          t2h[0] <= t2h[1]; t2h[1] <= t2h[2]; t2h[2] <= t2h[3];
          tn = tn - 3'd1;
        end
        if (ex_commit && !ldi_phase[te] && op == OP_SYS && fn6 == SYS_HPUT) begin
          if (hp_t2h_pop) t2h[t2h_n - 3'd1] <= rx_;
          else t2h[t2h_n[1:0]] <= rx_;
          tn = tn + 3'd1;
        end
        h2t_n <= hn;
        t2h_n <= tn;
      end

      // host control after the thread commit, so the host wins
      if (hp_ctl_v) begin
        for (q = 0; q < 4; q = q + 1) begin
          if (hp_ctl[q]) begin run[q] <= 1'b1; illegal[q] <= 1'b0; end
          if (hp_ctl[q + 4]) begin run[q] <= 1'b0; step[q] <= 1'b0; end
        end
      end
      if (hp_pc_v) pc[hp_pc_t] <= hp_pc;
      if (hp_step_v) begin run[hp_step_t] <= 1'b1; step[hp_step_t] <= 1'b1; end
    end
  end

  wire _unused = &{imm9, lane_flags, h_rd_capture, 1'b0};
endmodule
