// Free-running stand-in for tempo_lane in core proofs: every output is an
// unconstrained input to the solver, so core properties hold for any lane.
module tempo_lane #(parameter FULL = 1) (
    input  wire        clk, rst_n,
    input  wire [15:0] now, pins_in,
    input  wire        op_push,
    input  wire [2:0]  op_kind,
    input  wire [15:0] op_data,
    input  wire [4:0]  op_n,
    input  wire [2:0]  op_flags,
    input  wire [3:0]  op_reg,
    input  wire        op_pop, op_consume_start, op_consume_stop,
    input  wire [15:0] op_clear,
    input  wire        op_stop,
    input  wire [3:0]  rd_idx,
    output wire [15:0] rd_data, status,
    output wire        q_full, idle, rx_avail,
    output wire [15:0] rx_word,
    output wire        rx_end, rx_err, ev_start_o, ev_stop_o,
    output wire [15:0] cap_o, pins_cfg,
    output wire [1:0]  d_o, a_o, c_o,
    output wire [2:0]  owned_o,
    output wire        running_o
);
  (* anyseq *) wire [15:0] a1, a2, a3, a4, a5;
  (* anyseq *) wire [15:0] b;
  assign rd_data = a1; assign status = a2; assign rx_word = a3; assign cap_o = a4; assign pins_cfg = a5;
  assign {q_full, idle, rx_avail, rx_end, rx_err, ev_start_o, ev_stop_o, d_o, a_o, c_o, owned_o, running_o} = b[15:0];
endmodule
