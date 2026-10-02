package com.example.booking.reservations;

import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;

@Entity
public class Reservation {
    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;
    private String guestId;
    private String roomCode;
    private String status;

    protected Reservation() { }

    public Reservation(String guestId, String roomCode) {
        this.guestId = guestId;
        this.roomCode = roomCode;
        this.status = "HELD";
    }

    public String getGuestId() { return guestId; }
    public String getRoomCode() { return roomCode; }
    public String getStatus() { return status; }
}
