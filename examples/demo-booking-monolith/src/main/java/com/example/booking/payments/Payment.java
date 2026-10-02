package com.example.booking.payments;

import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;

@Entity
public class Payment {
    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;
    private String reservationRoom;
    private String guestId;
    private String status;

    protected Payment() { }

    public Payment(String reservationRoom, String guestId) {
        this.reservationRoom = reservationRoom;
        this.guestId = guestId;
        this.status = "AUTHORIZED";
    }

    public String getReservationRoom() { return reservationRoom; }
    public String getGuestId() { return guestId; }
    public String getStatus() { return status; }
}
